import contextlib
import io
import logging
import os
import shlex
import subprocess
import tempfile
import time
import uuid

from sqlalchemy import select

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import TestRun, TestRunStatus

_logger = logging.getLogger(__name__)


def _format_argv(args):
    """Render an argv list as a copy-pasteable shell command."""
    return " ".join(shlex.quote(str(a)) for a in args)


def _log_captured_output(label, result, level):
    """Log a CompletedProcess's stdout/stderr at *level*, tail-truncated to
    keep worker logs manageable. Use DEBUG level if you want everything."""
    def _tail(text, max_bytes=2000):
        if not text:
            return "(empty)"
        if len(text) <= max_bytes:
            return text
        return f"... ({len(text) - max_bytes} bytes elided) ...\n{text[-max_bytes:]}"
    _logger.log(level, "[%s] stdout:\n%s", label, _tail(result.stdout))
    _logger.log(level, "[%s] stderr:\n%s", label, _tail(result.stderr))


def run_external(args, *, label, timeout=None, env=None, cwd=None):
    """subprocess.run + structured logging.

    Always logs the command at INFO and the exit code + elapsed time. On a
    non-zero exit also logs a tail of stdout/stderr at WARNING — so when an
    'opp_env install' or 'docker run' returns FAIL in 0.0s the worker's
    log shows *why* without having to fish through the coordinator.

    With DEBUG logging enabled (e.g. `opp_ci -v worker start ...`), the
    full output is logged on every call, success or not.

    Returns the CompletedProcess.
    """
    _logger.info("[%s] $ %s", label, _format_argv(args))
    start = time.time()
    result = subprocess.run(
        args, capture_output=True, text=True,
        timeout=timeout, env=env, cwd=cwd,
    )
    elapsed = time.time() - start

    if result.returncode == 0:
        _logger.info("[%s] exit=0 (%.1fs)", label, elapsed)
        if _logger.isEnabledFor(logging.DEBUG):
            _log_captured_output(label, result, logging.DEBUG)
    else:
        _logger.warning("[%s] exit=%d (%.1fs)", label, result.returncode, elapsed)
        _log_captured_output(label, result, logging.WARNING)
    return result


def find_existing_run(session, *, project, test_type, version=None, mode=None, git_ref=None,
                      os=None, os_version=None, compiler=None, compiler_version=None,
                      isolation=None, toolchain=None):
    """Return an existing TestRun with matching params and terminal status, or None.

    Matches on the full (project, version, test_type, mode, git_ref, os,
    os_version, compiler, compiler_version, isolation, toolchain) tuple. Two
    runs with different isolation or toolchain are considered different runs
    even if every other field matches — they test different execution paths.

    Only runs that have completed (PASS, FAIL, or ERROR) are considered —
    queued/running runs do not block a new submission.
    """
    query = (
        select(TestRun)
        .where(
            TestRun.project == project,
            TestRun.version == version,
            TestRun.test_type == test_type,
            TestRun.mode == mode,
            TestRun.git_ref == git_ref,
            TestRun.os == os,
            TestRun.os_version == os_version,
            TestRun.compiler == compiler,
            TestRun.compiler_version == compiler_version,
            TestRun.isolation == isolation,
            TestRun.toolchain == toolchain,
            TestRun.status.in_([TestRunStatus.PASS, TestRunStatus.FAIL, TestRunStatus.ERROR]),
        )
        .order_by(TestRun.id.desc())
        .limit(1)
    )
    return session.execute(query).scalar_one_or_none()

COMMAND_MAP = {
    "smoke": "opp_run_smoke_tests",
    "fingerprint": "opp_run_fingerprint_tests",
    "statistical": "opp_run_statistical_tests",
    "feature": "opp_run_feature_tests",
    "speed": "opp_run_speed_tests",
    "sanitizer": "opp_run_sanitizer_tests",
    "chart": "opp_run_chart_tests",
    "release": "opp_run_release_tests",
    "build": "opp_build_project",
    "opp": "opp_run_opp_tests",
    "all": "opp_run_all_tests",
}

# Mapping from test_type to the opp_repl function that runs it.
# Lazily imported to avoid pulling opp_repl at module load time.
_TEST_FUNCTIONS = None


def _get_test_functions():
    global _TEST_FUNCTIONS
    if _TEST_FUNCTIONS is None:
        from opp_repl.test.smoke import run_smoke_tests
        from opp_repl.test.fingerprint.task import run_fingerprint_tests
        from opp_repl.test.statistical import run_statistical_tests
        from opp_repl.test.feature import run_feature_tests
        from opp_repl.test.speed.task import run_speed_tests
        from opp_repl.test.sanitizer import run_sanitizer_tests
        from opp_repl.test.chart import run_chart_tests
        from opp_repl.test.release import run_release_tests
        from opp_repl.test.opp import run_opp_tests
        from opp_repl.test.all import run_all_tests
        from opp_repl.simulation.build import build_project
        _TEST_FUNCTIONS = {
            "smoke": run_smoke_tests,
            "fingerprint": run_fingerprint_tests,
            "statistical": run_statistical_tests,
            "feature": run_feature_tests,
            "speed": run_speed_tests,
            "sanitizer": run_sanitizer_tests,
            "chart": run_chart_tests,
            "release": run_release_tests,
            "opp": run_opp_tests,
            "all": run_all_tests,
            "build": build_project,
        }
    return _TEST_FUNCTIONS


def _load_workspace(project_name, opp_file=None):
    """Create a SimulationWorkspace and resolve the simulation project.

    Project resolution order:
      1. Bundled ``@opp`` registry.
      2. Explicit *opp_file*, if given.
      3. Auto-discover any ``*.opp`` files in the current working directory
         (which inside an opp-ci-runner container is /work, the project root).
      4. Fall back to a programmatic ``define_simulation_project`` rooted at
         the cwd — for opp_env projects without an .opp file, opp_repl's
         defaults (ned/cpp/include/ini folders all = ".") let it build and
         test from a bare source tree.
    """
    from opp_repl.simulation.workspace import SimulationWorkspace
    ws = SimulationWorkspace()
    ws.load_opp_file("@opp")
    if opp_file:
        ws.load_opp_file(opp_file)

    cwd = os.getcwd()
    if os.path.isdir(cwd):
        for entry in os.listdir(cwd):
            if entry.endswith(".opp"):
                ws.load_opp_file(os.path.join(cwd, entry))

    try:
        simulation_project = ws.determine_default_simulation_project(name=project_name)
    except KeyError:
        _logger.info(
            "No .opp file defines project %r — registering programmatically "
            "with root_folder=%s", project_name, cwd,
        )
        simulation_project = ws.define_simulation_project(name=project_name, root_folder=cwd)
    return ws, simulation_project


def resolve_git_project(project, git_ref, *, toolchain="none"):
    """
    Resolve the opp_env project name and git ref for testing.

    Under toolchain=nix, a specific git_ref is realized by switching to the
    project's '-git' variant (e.g. inet-git) and pinning the commit via the
    OPP_ENV_GIT_REF env var. Under other toolchains the project name is left
    untouched.

    Returns (effective_project, effective_ref) tuple.
    """
    if not git_ref:
        return project, None
    if toolchain == "nix":
        effective_project = f"{project}-git" if not project.endswith("-git") else project
        return effective_project, git_ref
    return project, git_ref


def _remove_git_worktree(worktree_path):
    """Remove a git worktree directory."""
    try:
        git_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        if git_root_result.returncode != 0:
            _logger.warning("Could not determine git root for worktree %s", worktree_path)
            return
        # Find the main repo root (worktree's commondir)
        common_result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        if common_result.returncode == 0:
            main_git_dir = os.path.abspath(os.path.join(
                worktree_path, common_result.stdout.strip(),
            ))
            main_repo = os.path.dirname(main_git_dir)
        else:
            main_repo = worktree_path
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=main_repo, capture_output=True, text=True,
        )
        _logger.info("Removed worktree %s", worktree_path)
    except (OSError, FileNotFoundError) as e:
        _logger.warning("Failed to remove worktree %s: %s", worktree_path, e)


def resolve_commit_sha(project, opp_file=None):
    """
    Resolve the current HEAD SHA for a project.

    Uses the opp_file's parent directory if provided, otherwise falls back
    to OPP_CI_PROJECT_DIR env vars.

    Returns the 40-char commit hash, or None if it cannot be determined.
    """
    if opp_file:
        project_dir = os.path.dirname(os.path.abspath(opp_file))
    else:
        env_key = f"OPP_CI_PROJECT_DIR_{project.upper().replace('-', '_')}"
        project_dir = os.environ.get(env_key)
        if not project_dir:
            base_dir = os.environ.get("OPP_CI_PROJECT_DIR", ".")
            project_dir = os.path.join(base_dir, project)

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, FileNotFoundError):
        pass
    _logger.debug("Could not resolve commit SHA for %s", project)
    return None


def install_project(project, git_ref=None, *, isolation="none", toolchain="none"):
    """Install a project via opp_env on the worker's host.

    Only meaningful when ``isolation=none`` and ``toolchain=nix``:
      - isolation=docker → install happens inside the container's entrypoint
      - toolchain=none   → project is expected to be pre-built on the host
    """
    if isolation != "none" or toolchain != "nix":
        _logger.info("Skipping install (isolation=%s, toolchain=%s)", isolation, toolchain)
        return

    effective_project, _ = resolve_git_project(project, git_ref, toolchain="nix")
    result = run_external(
        ["opp_env", "install", effective_project],
        label=f"opp_env install {effective_project}",
    )
    if result.returncode != 0:
        raise RuntimeError(f"opp_env install {effective_project} failed (exit code {result.returncode})")
    _logger.info("Installation of %s complete", effective_project)


def run_test(project, test_type, *, isolation=None, toolchain=None, **kwargs):
    """
    Run a test for the given project, dispatching on isolation × toolchain.

      isolation=docker          → _run_test_in_docker (wraps the inner path)
      isolation=none, tc=nix    → _run_test_via_opp_env  (opp_env on host)
      isolation=none, tc=none   → _run_test_direct       (opp_repl in-process)

    Extra kwargs (git_ref, opp_file, mode, os, os_version, compiler,
    compiler_version) are passed through; helpers extract what they need.

    Returns a dict with keys: result_code, duration_seconds, stdout, stderr,
    details, commit_sha.
    """
    isolation = isolation or "none"
    toolchain = toolchain or "none"
    if isolation == "docker":
        return _run_test_in_docker(project, test_type, toolchain=toolchain, **kwargs)
    if toolchain == "nix":
        return _run_test_via_opp_env(project, test_type, **kwargs)
    return _run_test_direct(project, test_type, **kwargs)


def _opp_cache_root():
    """Return the directory where the worker keeps cloned project sources."""
    base = os.environ.get("OPP_CI_CACHE_DIR")
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".cache", "opp_ci", "clones")


def _parse_opp_file_kwargs(opp_file):
    """Parse a .opp file via opp_repl's restricted-AST parser.

    Returns the SimulationProject/OmnetppProject keyword-argument dict, or
    None if the file can't be parsed (missing, syntax error). Relative path
    values inside the .opp are resolved against the file's directory.
    """
    if not opp_file or not os.path.isfile(opp_file):
        return None
    try:
        from opp_repl.simulation.workspace import _parse_opp_file, _resolve_opp_paths
    except ImportError:
        _logger.debug("opp_repl not importable — falling back to opp_file dirname")
        return None
    try:
        _class_name, kwargs = _parse_opp_file(opp_file)
    except (OSError, ValueError) as e:
        _logger.warning("Could not parse %s: %s", opp_file, e)
        return None
    _resolve_opp_paths(opp_file, kwargs)
    return kwargs


def _ensure_github_clone(owner, repo):
    """Clone github.com/<owner>/<repo> into the worker's cache (or fetch
    into an existing clone) and return the absolute path. The clone is
    left at origin/HEAD — callers that need a specific ref should create
    a worktree via :py:func:`_create_git_worktree`.
    """
    cache_root = _opp_cache_root()
    os.makedirs(cache_root, exist_ok=True)
    target = os.path.join(cache_root, f"{owner}__{repo}")
    url = f"https://github.com/{owner}/{repo}.git"
    if not os.path.isdir(os.path.join(target, ".git")):
        result = run_external(
            ["git", "clone", url, target],
            label=f"git clone {owner}/{repo}", timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone {url} failed: {result.stderr.strip()}")
    else:
        run_external(
            ["git", "fetch", "--prune", "origin"],
            label=f"git fetch {owner}/{repo}", cwd=target, timeout=120,
        )
    return target


def _resolve_project_dir(project, opp_file=None):
    """Resolve the on-disk source dir for *project*.

    Resolution order:
      1. Parse the .opp file (if given). If it sets
         ``github_owner`` + ``github_repository``, clone (or fetch) that
         repo into the worker cache and return the clone path. The clone
         is at origin/HEAD; ref-specific checkouts happen later via
         :py:func:`_create_git_worktree`.
      2. If the .opp sets ``root_folder``, return that absolute path.
      3. The .opp file's own parent directory.
      4. ``$OPP_CI_PROJECT_DIR_<PROJECT>``.
      5. ``$OPP_CI_PROJECT_DIR/<project>``.

    The ``opp_env_project`` axis is handled separately by ``install_project``
    (which calls ``opp_env install``) — it is not the source dir.
    """
    kwargs = _parse_opp_file_kwargs(opp_file) if opp_file else None
    if kwargs:
        owner = kwargs.get("github_owner")
        repo = kwargs.get("github_repository")
        if owner and repo:
            return _ensure_github_clone(owner, repo)
        root = kwargs.get("root_folder")
        if root:
            return os.path.abspath(root)
    if opp_file:
        return os.path.dirname(os.path.abspath(opp_file))
    env_key = f"OPP_CI_PROJECT_DIR_{project.upper().replace('-', '_')}"
    project_dir = os.environ.get(env_key)
    if project_dir:
        return project_dir
    base_dir = os.environ.get("OPP_CI_PROJECT_DIR", ".")
    return os.path.join(base_dir, project)


def _create_git_worktree(project_dir, git_ref):
    """Create a detached git worktree for *git_ref* in a temp dir and return its path."""
    target = os.path.join(tempfile.gettempdir(), f"opp-ci-worktree-{uuid.uuid4().hex[:8]}")
    run_external(
        ["git", "fetch", "origin"], label="git fetch", cwd=project_dir, timeout=120,
    )
    result = run_external(
        ["git", "worktree", "add", "--detach", target, git_ref],
        label=f"git worktree add {git_ref}", cwd=project_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add {git_ref} failed: {result.stderr.strip()}")
    _logger.info("Created worktree at %s for ref %s", target, git_ref)
    return target


def _docker_image_tag(toolchain, os_name, os_version, compiler, compiler_version,
                      omnetpp_version=None):
    """Compute the runner image tag for a given combination.

    Naming convention:
      toolchain=nix:  opp-ci-runner:nix-<os>-<osver>
      toolchain=none: opp-ci-runner:host-<os>-<osver>-<compiler>-<compver>-omnetpp-<ompver>

    The host-toolchain image has a specific OMNeT++ version baked in (built
    via opp_env install --nixless-workspace at image-build time), so a
    different omnetpp version means a different image.
    """
    if not os_name or not os_version:
        raise ValueError("isolation=docker requires both 'os' and 'os_version' to be set on the run")
    os_slug = f"{os_name.lower()}-{os_version}"
    if toolchain == "nix":
        return f"opp-ci-runner:nix-{os_slug}"
    if not compiler or not compiler_version:
        raise ValueError(
            "isolation=docker with toolchain=none requires both 'compiler' and 'compiler_version'"
        )
    if not omnetpp_version:
        raise ValueError(
            "isolation=docker with toolchain=none requires an omnetpp version "
            "(set resolved_deps['omnetpp'] on the run, or pick one in the New Run form)"
        )
    return f"opp-ci-runner:host-{os_slug}-{compiler.lower()}-{compiler_version}-omnetpp-{omnetpp_version}"


def _resolve_remote_head(url, ref="HEAD"):
    """Return the SHA that *ref* points to at the remote, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    return line[0].split()[0]


_OPP_CI_REPO = "https://github.com/omnetpp/opp_ci.git"
_OPP_REPL_REPO = "https://github.com/omnetpp/opp_repl.git"
_OPP_ENV_REPO = "https://github.com/omnetpp/opp_env.git"


def render_dockerfile(toolchain, os_name, os_version, compiler, compiler_version,
                      omnetpp_version=None):
    """Render the Dockerfile (and, for host toolchain, the entrypoint script)
    for one runner-image combination.

    Returns a dict ``{filename: rendered_content}`` — at minimum
    ``{"Dockerfile": "..."}``, plus ``{"opp_ci_entry.sh": "..."}`` for the
    host toolchain. The caller writes each file into the build context.

    For the host template:
      * Current HEAD SHA of opp_env is resolved via 'git ls-remote' and
        baked into the pip-install line. opp_ci and opp_repl are *not*
        baked into the image — the entrypoint clones them at container
        start, so a push to either does not invalidate this image.
      * *omnetpp_version* is required: it's installed via opp_env at
        build time using a nixless workspace.
    """
    import importlib.resources
    from jinja2 import Environment, FileSystemLoader
    import yaml

    template_name = "host" if toolchain == "none" else toolchain

    docker_dir = importlib.resources.files("opp_ci").joinpath("docker")
    jenv = Environment(loader=FileSystemLoader(str(docker_dir)),
                       keep_trailing_newline=True)
    ctx = {
        "os": os_name.lower(),
        "os_version": os_version,
        "compiler": compiler.lower() if compiler else None,
        "compiler_version": compiler_version,
    }
    if template_name == "host":
        if not omnetpp_version:
            raise ValueError("host-toolchain image requires omnetpp_version")
        ctx["omnetpp_version"] = omnetpp_version
        pkgs_path = docker_dir.joinpath("packages.yml")
        with open(pkgs_path) as f:
            pkg_map = yaml.safe_load(f) or {}
        key = f"{ctx['os']}+{ctx['compiler']}-{ctx['compiler_version']}"
        ctx["compiler_package"] = pkg_map.get(key, f"{ctx['compiler']}-{ctx['compiler_version']}")
        ctx["opp_env_ref"] = _resolve_remote_head(_OPP_ENV_REPO) or "HEAD"

    files = {"Dockerfile": jenv.get_template(f"Dockerfile.{template_name}.j2").render(**ctx)}
    if template_name == "host":
        files["opp_ci_entry.sh"] = jenv.get_template("opp_ci_entry.sh.j2").render(**ctx)
    return files


def _image_exists_locally(tag):
    """True iff 'docker image inspect <tag>' succeeds (image is in the local store)."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "docker binary not found on PATH — install Docker on this worker, "
            "or untag the worker as docker-capable."
        )
    return result.returncode == 0


def build_runner_image(tag, toolchain, os_name, os_version, compiler, compiler_version,
                       *, omnetpp_version=None, push=False):
    """Build (and optionally push) one opp-ci-runner image.

    For host toolchain, *omnetpp_version* is required — the Dockerfile uses
    'opp_env install --nixless-workspace' at build time to bake that specific
    OMNeT++ into the image so the container can run opp_repl tests without
    needing OMNeT++ at run time.
    """
    files = render_dockerfile(
        toolchain, os_name, os_version, compiler, compiler_version,
        omnetpp_version=omnetpp_version,
    )
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(content)
        dockerfile_path = os.path.join(tmp, "Dockerfile")
        result = run_external(
            ["docker", "build", "-t", tag, "-f", dockerfile_path, tmp],
            label=f"docker build {tag}",
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker build {tag} failed (exit {result.returncode})")
    if push:
        result = run_external(["docker", "push", tag], label=f"docker push {tag}")
        if result.returncode != 0:
            raise RuntimeError(f"docker push {tag} failed (exit {result.returncode})")


def _ensure_runner_image(tag, toolchain, os_name, os_version, compiler, compiler_version,
                         *, omnetpp_version=None):
    """Invoke 'docker build' for the image so docker's layer cache picks up
    any changes — new pinned SHAs from an upstream push, edits to the
    Dockerfile template, a different compiler package, a different OMNeT++
    version, etc.

    When nothing relevant has changed, every layer hits the cache and the
    call finishes in a couple of seconds. When something has changed, only
    the affected layers (and downstream ones) actually rebuild.
    """
    _logger.info("Ensuring image %s is up to date", tag)
    build_runner_image(
        tag, toolchain, os_name, os_version, compiler, compiler_version,
        omnetpp_version=omnetpp_version,
    )


def _run_test_in_docker(project, test_type, *, toolchain="none", **kwargs):
    """Run a test inside a Docker container.

    Two flavours, distinguished by whether the matrix has an opp_file:

    * **SimulationProject** (opp_file set) — bind-mount the project source
      tree to ``/work``. When *git_ref* is set, a detached worktree is
      created on the host first so the container always sees a clean
      checkout at the requested commit. The worktree is removed after.
    * **opp_env catalog** (opp_file absent) — no bind-mount; the entrypoint
      runs ``opp_env install`` for the project inside the container (env
      var ``OPP_CI_INSTALL_PROJECTS``) and ``cd``s into its install dir.
      A scratch tmpdir is still mounted at ``/work`` so WORKDIR resolves.

    For toolchain=nix, the host's Nix store is shared via a named volume to
    avoid re-downloading deps per run.
    """
    git_ref = kwargs.get("git_ref")
    opp_file = kwargs.get("opp_file")
    mode = kwargs.get("mode")
    os_name = kwargs.get("os")
    os_version = kwargs.get("os_version")
    compiler = kwargs.get("compiler")
    compiler_version = kwargs.get("compiler_version")
    resolved_deps = kwargs.get("resolved_deps") or {}
    omnetpp_version = resolved_deps.get("omnetpp") if isinstance(resolved_deps, dict) else None

    image = _docker_image_tag(
        toolchain, os_name, os_version, compiler, compiler_version,
        omnetpp_version=omnetpp_version,
    )
    _ensure_runner_image(
        image, toolchain, os_name, os_version, compiler, compiler_version,
        omnetpp_version=omnetpp_version,
    )

    is_catalog = not opp_file
    worktree_path = None
    scratch_dir = None
    if is_catalog:
        # Source comes from `opp_env install` inside the container. We still
        # mount *something* at /work so the image's WORKDIR resolves; an
        # empty tmpdir is fine because the entrypoint cd's away from it.
        scratch_dir = tempfile.mkdtemp(prefix="opp-ci-catalog-")
        mount_path = scratch_dir
    else:
        project_dir = _resolve_project_dir(project, opp_file)
        if git_ref:
            worktree_path = _create_git_worktree(project_dir, git_ref)
            mount_path = worktree_path
        else:
            mount_path = project_dir

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{os.path.abspath(mount_path)}:/work",
        "-w", "/work",
    ]
    if is_catalog:
        # The entrypoint reads this, opp_env-installs each project, and cd's
        # into the first one's install dir.
        docker_cmd += ["-e", f"OPP_CI_INSTALL_PROJECTS={project}"]
    # The image's ENTRYPOINT is the runner binary (opp_env / opp_ci), so we
    # only need to pass its arguments here — repeating the binary name would
    # produce `opp_ci opp_ci ...` and "No such command" from click.
    if toolchain == "nix":
        docker_cmd += ["-v", "opp-ci-nix-store:/nix"]
        if git_ref:
            docker_cmd += ["-e", f"OPP_ENV_GIT_REF={git_ref}"]
        effective_project, _ = resolve_git_project(project, git_ref, toolchain="nix")
        inner_cmd = COMMAND_MAP.get(test_type)
        if inner_cmd is None:
            raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(COMMAND_MAP.keys())}")
        if mode:
            inner_cmd += f" --mode {mode}"
        container_args = ["run", effective_project, "-c", inner_cmd]
    else:
        container_args = ["internal", "run-direct",
                          "--project", project, "--test-type", test_type]
        if mode:
            container_args += ["--mode", mode]
        if opp_file:
            container_args += ["--opp-file", "/work/" + os.path.basename(opp_file)]
        # Catalog runs deliberately omit --opp-file: opp_repl's _load_workspace
        # auto-discovers any .opp in cwd (the install dir, set by entrypoint),
        # else falls back to a default SimulationProject rooted there.

    docker_cmd.append(image)
    docker_cmd += container_args

    start = time.time()
    try:
        result = run_external(docker_cmd, label=f"docker:{image}")
        duration = time.time() - start
    finally:
        if worktree_path:
            _remove_git_worktree(worktree_path)
        if scratch_dir:
            import shutil
            shutil.rmtree(scratch_dir, ignore_errors=True)

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "details": None,
        "commit_sha": git_ref,
    }


def _run_test_via_opp_env(project, test_type, **kwargs):
    """Run a test via opp_env subprocess (Nix environment on the host)."""
    git_ref = kwargs.get("git_ref")
    mode = kwargs.get("mode")

    cmd = COMMAND_MAP.get(test_type)
    if cmd is None:
        raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(COMMAND_MAP.keys())}")

    if mode:
        cmd += f" --mode {mode}"

    env = os.environ.copy()
    effective_project, effective_ref = resolve_git_project(project, git_ref, toolchain="nix")
    if effective_ref:
        env["OPP_ENV_GIT_REF"] = effective_ref
    args = ["opp_env", "run", effective_project, "-c", cmd]

    start = time.time()
    result = run_external(args, label=f"opp_env:{effective_project}", env=env)
    duration = time.time() - start

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "details": None,
        "commit_sha": None,
    }


def _run_test_direct(project, test_type, *, opp_file=None, git_ref=None, mode=None, **_unused):
    """Run a test by calling opp_repl functions directly (no subprocess).

    When *git_ref* is set, an isolated git worktree is created for that
    commit and removed after the test completes.

    Extra kwargs (e.g. os, compiler) are accepted-and-ignored so this can
    sit downstream of the run_test dispatcher.
    """
    test_functions = _get_test_functions()
    func = test_functions.get(test_type)
    if func is None:
        raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(test_functions.keys())}")

    _ws, simulation_project = _load_workspace(project, opp_file)

    worktree_path = None
    if git_ref:
        from opp_repl.simulation.project import make_worktree_simulation_project
        root = simulation_project.get_root_path()
        if root:
            subprocess.run(["git", "fetch", "origin"], cwd=root,
                           capture_output=True, timeout=120)
        simulation_project = make_worktree_simulation_project(simulation_project, git_ref)
        worktree_path = simulation_project.get_root_path()
        _logger.info("Created worktree at %s for %s@%s", worktree_path, project, git_ref)

    from opp_repl.common.util import ensure_logging_initialized
    ensure_logging_initialized("DEBUG", "DEBUG", None)

    _logger.info("Running %s test for %s (direct mode)", test_type, project)
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    start = time.time()
    try:
        call_kwargs = {"simulation_project": simulation_project, "build": "task", "build_mode": "task"}
        if mode:
            call_kwargs["mode"] = mode
        if test_type == "opp":
            call_kwargs["test_folder"] = simulation_project.get_full_path(".")
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            result = func(**call_kwargs)
            if result is not None:
                print(repr(result))
        duration = time.time() - start

        if result is None:
            result_code = "PASS"
            details = None
        elif hasattr(result, "is_all_results_expected"):
            result_code = "PASS" if result.is_all_results_expected() else "FAIL"
            details = result.to_dict() if hasattr(result, "to_dict") else None
        else:
            result_code = "PASS"
            details = None

    except Exception as e:
        duration = time.time() - start
        _logger.error("Test %s raised exception: %s", test_type, e)
        return {
            "result_code": "ERROR",
            "duration_seconds": duration,
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue() + "\n" + repr(e),
            "details": None,
            "commit_sha": git_ref,
        }
    finally:
        if worktree_path:
            _remove_git_worktree(worktree_path)

    commit_sha = git_ref or resolve_commit_sha(project, opp_file=opp_file)
    _logger.info("Test finished: %s (%.1fs)", result_code, duration)
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "details": details,
        "commit_sha": commit_sha,
    }
