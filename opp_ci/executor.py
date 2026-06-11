import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid

from opp_ci.stages import Stage

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


class _CallbackStringIO(io.StringIO):
    """StringIO that also forwards each completed line to ``on_output``.

    Used for the in-process test path (`_run_test_direct`), where output is
    captured by redirecting stdout/stderr into a buffer rather than read from
    a subprocess. Subclassing StringIO keeps ``getvalue()`` working, so the
    final captured output is unchanged; the callback just gets a live copy.
    """

    def __init__(self, on_output):
        super().__init__()
        self._on_output = on_output
        self._pending = ""

    def write(self, s):
        n = super().write(s)
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            try:
                self._on_output(line)
            except Exception:  # noqa: BLE001 — live streaming is best-effort
                pass
        return n


def _pump_stream(stream, sink, emit, stream_name):
    """Read *stream* line by line, append each line to *sink* and pass it to
    *emit* tagged with *stream_name* ("out"/"err"). Runs on its own thread so
    stdout and stderr can be teed concurrently without one blocking the
    other."""
    try:
        for line in iter(stream.readline, ""):
            sink.append(line)
            emit(line.rstrip("\n"), stream_name)
    finally:
        stream.close()


def _run_external_streaming(args, *, label, timeout, env, cwd, start, on_output=None):
    """Popen variant of :py:func:`run_external` that logs the child's output
    line by line *as it is produced* (each line at INFO, prefixed with
    *label*), so a long ``podman build`` or ``opp_env`` compile shows live
    progress in the worker's journal instead of one tail dump at the end.

    stdout and stderr are teed on separate threads, so the returned
    CompletedProcess keeps them separate just like ``subprocess.run`` —
    callers that store ``result.stdout`` / ``result.stderr`` are unaffected.
    """
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env, cwd=cwd,
    )
    out_lines, err_lines = [], []

    def emit(line, stream_name):
        _logger.info("[%s] %s", label, line)
        # Tee to the live per-run output stream when the caller wants it,
        # tagged with the stream it came from. Called from both pump threads,
        # so on_output must be thread-safe; never let a hiccup break the run.
        if on_output is not None:
            try:
                on_output(stream_name, line)
            except Exception:  # noqa: BLE001
                pass
    threads = [
        threading.Thread(target=_pump_stream, args=(proc.stdout, out_lines, emit, "out")),
        threading.Thread(target=_pump_stream, args=(proc.stderr, err_lines, emit, "err")),
    ]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in threads:
            t.join()
        raise subprocess.TimeoutExpired(
            args, timeout, output="".join(out_lines), stderr="".join(err_lines),
        )
    for t in threads:
        t.join()
    elapsed = time.time() - start
    if proc.returncode == 0:
        _logger.info("[%s] exit=0 (%.1fs)", label, elapsed)
    else:
        _logger.warning("[%s] exit=%d (%.1fs)", label, proc.returncode, elapsed)
    return subprocess.CompletedProcess(
        args, proc.returncode,
        stdout="".join(out_lines), stderr="".join(err_lines),
    )


def run_external(args, *, label, timeout=None, env=None, cwd=None, stream=False,
                 on_output=None):
    """subprocess.run + structured logging.

    Always logs the command at INFO and the exit code + elapsed time. On a
    non-zero exit also logs a tail of stdout/stderr at WARNING — so when an
    'opp_env install' or 'podman run' returns FAIL in 0.0s the worker's
    log shows *why* without having to fish through the coordinator.

    With DEBUG logging enabled (e.g. `opp_ci -v worker start ...`), the
    full output is logged on every call, success or not.

    With *stream=True* the child's output is teed to the log line by line as
    it runs (see :py:func:`_run_external_streaming`) — use it for the slow
    commands (podman build, the container run, opp_env compile) so the admin
    can watch the build/compile progress in the journal rather than waiting
    for one dump at the end.

    Returns the CompletedProcess.
    """
    _logger.info("[%s] $ %s", label, _format_argv(args))
    start = time.time()
    if stream:
        return _run_external_streaming(
            args, label=label, timeout=timeout, env=env, cwd=cwd, start=start,
            on_output=on_output,
        )
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

# Mapping from test name to the opp_repl function that runs it.
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


# A name already carries an explicit version when it ends in a numeric version
# (e.g. inet-4.5), or the pseudo-versions "latest" / "git".
_VERSIONED_NAME_RE = re.compile(r"^(.*?)-(\d.*|latest|git)$")


def resolve_opp_env_id(project, git_ref=None, *, toolchain="nix"):
    """Resolve the *versioned* opp_env project identifier for install/run.

    opp_env rejects a bare catalog name like 'mm1k' as ambiguous ("Which
    version of 'mm1k' do you mean?"); it needs a versioned identifier. The
    rules mirror the podman entrypoint's catalog-name split:

      - a specific git_ref under nix → the '-git' variant (the caller pins the
        commit via the OPP_ENV_GIT_REF env var)
      - an already-versioned name (inet-4.5, mm1k-latest, foo-git) → kept as-is
      - a bare name → the '-latest' alias

    Returns (effective_project, effective_ref).
    """
    effective_project, effective_ref = resolve_git_project(project, git_ref, toolchain=toolchain)
    if effective_ref:
        return effective_project, effective_ref
    if _VERSIONED_NAME_RE.match(effective_project):
        return effective_project, None
    return f"{effective_project}-latest", None


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


def install_project(project, git_ref=None, *, isolation="none", toolchain="none",
                    resolved_deps=None, compiler=None, compiler_version=None,
                    recorder=None):
    """Install a project via opp_env on the worker's host.

    Only meaningful when ``isolation=none`` and ``toolchain=nix``:
      - isolation=podman → install happens inside the container's entrypoint
      - toolchain=none   → project is expected to be pre-built on the host

    The remaining keyword args (``resolved_deps``, ``compiler``,
    ``compiler_version``) plus *project* and *git_ref* form the build
    coordinate that picks the per-coordinate workspace; they must match the
    axes the run step passes to :py:func:`_opp_env_workspace` so install and
    run land in the *same* directory (omnetpp built once, reused by the run).

    When ``recorder`` is given and the install actually runs (host-nix), it is
    captured as a ``deps.install`` stage; on the no-op paths no stage is added.
    """
    if isolation != "none" or toolchain != "nix":
        _logger.info("Skipping install (isolation=%s, toolchain=%s)", isolation, toolchain)
        return

    effective_project, _ = resolve_opp_env_id(project, git_ref, toolchain="nix")
    ws = _opp_env_workspace(
        project=project, resolved_deps=resolved_deps, toolchain="nix",
        compiler=compiler, compiler_version=compiler_version, git_ref=git_ref,
    )
    _gc_workspaces()
    # --init marks the workspace on first use (same flag the host Containerfile
    # uses); it is a no-op once the workspace exists, so reuse is idempotent.
    argv = ["opp_env", "install", "--init", effective_project]
    if recorder is not None:
        recorder.begin(Stage.DEPS_INSTALL, command=_format_argv(argv))
    with _workspace_lock(ws):
        result = run_external(
            argv, label=f"opp_env install {effective_project}", cwd=ws,
            stream=True, on_output=recorder.output if recorder else None,
        )
    if recorder is not None:
        recorder.end(result.returncode)
    if result.returncode != 0:
        raise RuntimeError(f"opp_env install {effective_project} failed (exit code {result.returncode})")
    _logger.info("Installation of %s complete in %s", effective_project, ws)


def run_test(project, kind, *, isolation=None, toolchain=None, recorder=None, **kwargs):
    """
    Run a test for the given project, dispatching on isolation × toolchain.

      isolation=podman          → _run_test_in_podman (wraps the inner path)
      isolation=none, tc=nix    → _run_test_via_opp_env  (opp_env on host)
      isolation=none, tc=none   → _run_test_direct       (opp_repl in-process)

    Extra kwargs (git_ref, opp_file, mode, os, os_version, compiler,
    compiler_version) are passed through; helpers extract what they need.

    ``recorder``, when given, is a :py:class:`opp_ci.stages.StageRecorder` the
    helpers drive to capture the run as ordered stages (project.build,
    test.run, …) and stream live events. It does not affect the returned
    stdout/stderr.

    Returns a dict with keys: result_code, test_exec_seconds, stdout, stderr,
    details, commit_sha.
    """
    isolation = isolation or "none"
    toolchain = toolchain or "none"
    if isolation == "podman":
        return _run_test_in_podman(project, kind, toolchain=toolchain,
                                   recorder=recorder, **kwargs)
    if toolchain == "nix":
        return _run_test_via_opp_env(project, kind, recorder=recorder, **kwargs)
    return _run_test_direct(project, kind, recorder=recorder, **kwargs)


def _opp_cache_root():
    """Return the directory where the worker keeps cloned project sources."""
    base = os.environ.get("OPP_CI_CACHE_DIR")
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".cache", "opp_ci", "clones")


def _opp_env_workspace(*, project, resolved_deps, toolchain, compiler,
                       compiler_version, git_ref):
    """Return (creating + touching it) the per-coordinate opp_env workspace dir.

    The host-nix path (isolation=none, toolchain=nix) has no container to
    isolate it: ``opp_env install``/``run`` compile omnetpp and the project
    *in-tree*, and the Nix store only content-addresses the toolchain and
    external libs, not that build. So two runs differing in any axis that
    changes the dependency closure (omnetpp pin, compiler, git ref, project)
    must not share a tree — else their builds clobber each other or two
    concurrent jobs race. Identical coordinate → same directory → omnetpp is
    built once and reused.

    The directory name is a legible prefix plus a stable hash of the full
    coordinate tuple; the hash is authoritative, the prefix is only there so
    ``ls <root>`` is readable. The dir's mtime is bumped on every resolution
    (install *and* run) so GC's LRU reflects actual use, not creation.
    """
    from opp_ci import config

    deps = resolved_deps if isinstance(resolved_deps, dict) else {}
    # Canonical, order-independent view of the coordinate: sorting keys and
    # normalising None→"" means dict iteration order or a missing axis can
    # never shift the hash between the install and run steps.
    coordinate = {
        "project": project or "",
        "toolchain": toolchain or "",
        "compiler": compiler or "",
        "compiler_version": str(compiler_version or ""),
        "git_ref": git_ref or "",
        "deps": {str(k): str(v) for k, v in sorted(deps.items())},
    }
    canon = json.dumps(coordinate, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha1(canon.encode()).hexdigest()[:8]

    omnetpp = deps.get("omnetpp")
    raw_prefix = "-".join(filter(None, [
        project or "proj",
        f"omnetpp{omnetpp}" if omnetpp else "omnetpp",
        f"{compiler}{compiler_version}" if compiler else (toolchain or "nix"),
        (git_ref or "")[:8] or "none",
    ]))
    prefix = re.sub(r"[^A-Za-z0-9._-]", "", raw_prefix)
    ws = os.path.join(config.WORKSPACE_ROOT, f"{prefix}-{digest}")
    os.makedirs(ws, exist_ok=True)
    try:
        os.utime(ws, None)  # LRU touch; reuse should look recent to GC
    except OSError:
        pass
    return ws


def _workspace_lock_path(ws):
    return os.path.join(ws, ".opp_ci.lock")


@contextlib.contextmanager
def _workspace_lock(ws):
    """Hold an exclusive flock on ``<ws>/.opp_ci.lock`` for the block.

    Serialises same-coordinate install/build so a re-run (or worker
    concurrency > 1) waits for the in-flight build instead of reading or
    corrupting a half-built tree. Different coordinates use different files
    and never contend. flock is advisory and Unix-only — the host-nix path
    only runs on Unix workers.
    """
    import fcntl

    os.makedirs(ws, exist_ok=True)
    fd = os.open(_workspace_lock_path(ws), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _gc_workspaces():
    """Evict least-recently-used workspace dirs beyond ``config.WORKSPACE_MAX``.

    Called before each install. The newest WORKSPACE_MAX directories (by
    mtime, bumped on each reuse) are kept; older ones are removed. A dir
    whose lock is currently held by a concurrent build is skipped, so GC
    never deletes a tree out from under a running job. Count-based, not
    age-based — kept deliberately simple.
    """
    import fcntl
    import shutil

    from opp_ci import config

    root = config.WORKSPACE_ROOT
    try:
        dirs = [os.path.join(root, n) for n in os.listdir(root)]
    except OSError:
        return
    dirs = [p for p in dirs if os.path.isdir(p)]
    if len(dirs) <= config.WORKSPACE_MAX:
        return
    dirs.sort(key=lambda p: os.path.getmtime(p))  # oldest first
    for ws in dirs[:-config.WORKSPACE_MAX]:
        try:
            fd = os.open(_workspace_lock_path(ws), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _logger.debug("Skipping GC of in-use workspace %s", ws)
                continue
            shutil.rmtree(ws, ignore_errors=True)
            _logger.info("Evicted LRU opp_env workspace %s", ws)
        finally:
            os.close(fd)


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


# Named volume holding the shared Nix store, mounted at /nix for every nix
# run and during the nix image bake. Baked nix images reference /nix/store
# paths that live in THIS volume, so it must not be garbage-collected — see
# the warning in Containerfile.nix.j2.
_NIX_STORE_VOLUME = "opp-ci-nix-store"


def _normalize_toolchain(toolchain):
    """Canonical toolchain token for tags/labels: 'nix' or 'none'.

    The codebase uses both 'host' (CLI) and 'none' (executor) for the
    OS-package-manager compiler; both map to 'none' in the image identity.
    """
    return "nix" if toolchain == "nix" else "none"


def _runner_image_tag(slug, toolchain, compiler, compiler_version, omnetpp_version):
    """Build the runner image tag from already-resolved dimensions (scheme B).

    A single uniform template carrying only the *pinned & baked* dimensions:

        opp-ci-runner:<slug>-<toolchain>[-<compiler>-<compiler_version>]-omnetpp-<omnetpp_version>

    - ``<toolchain>`` is ``none`` (compiler from the OS package manager) or
      ``nix`` (compiler/deps from opp_env+Nix).
    - the ``<compiler>-<compiler_version>`` segment appears only for ``none``;
      under nix opp_env selects the compiler, so it is not a pinned dimension.
    - ``omnetpp`` is baked into both flavours (host via --nixless-workspace,
      nix via run+commit), so it is always present.

    The same dimensions are also attached as ``org.opp_ci.*`` labels
    (see ``_runner_image_labels``).
    """
    tc = _normalize_toolchain(toolchain)
    if not omnetpp_version:
        raise ValueError("runner image tag requires an omnetpp version")
    parts = [slug, tc]
    if tc == "none":
        if not compiler or not compiler_version:
            raise ValueError(
                "host (toolchain=none) runner image tag requires compiler and compiler_version"
            )
        parts += [compiler.lower(), str(compiler_version)]
    parts += ["omnetpp", omnetpp_version]
    return "opp-ci-runner:" + "-".join(parts)


def _runner_image_labels(slug, toolchain, compiler, compiler_version, omnetpp_version):
    """The key=value form of an image's dimensions, as org.opp_ci.* labels.

    Mirrors ``_runner_image_tag`` but stored where ``=`` is legal, so the
    full self-describing view is queryable via
    ``podman images --filter label=org.opp_ci.omnetpp=6.4.0`` and inspectable.
    """
    tc = _normalize_toolchain(toolchain)
    labels = {
        "org.opp_ci.platform": slug,
        "org.opp_ci.toolchain": tc,
        "org.opp_ci.omnetpp": omnetpp_version or "",
    }
    if tc == "none" and compiler:
        labels["org.opp_ci.compiler"] = compiler.lower()
        labels["org.opp_ci.compiler-version"] = str(compiler_version or "")
    return labels


def _nix_base_image_tag(slug):
    """Tag of the omnetpp-agnostic nix base image (nix + opp_env, empty
    workspace). The per-omnetpp runner image is committed from a container of
    this base after ``opp_env install omnetpp-<ver>``."""
    return f"opp-ci-runner-base:{slug}-nix"


def _resolve_platform_slug(os_name, os_version, *, distro, distro_version,
                           flavor, flavor_version):
    from opp_ci import platforms
    slug = platforms.platform_slug(
        os=os_name, os_version=os_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
    )
    if not slug or "-" not in slug:
        raise ValueError(
            "isolation=podman requires a fully-specified platform "
            "(os+os_version, or distro+distro_version, or flavor+version)"
        )
    return slug


def _podman_image_tag(toolchain, os_name, os_version, compiler, compiler_version,
                      *, distro=None, distro_version=None,
                      flavor=None, flavor_version=None,
                      omnetpp_version=None):
    """Compute the runner image tag for a given combination (scheme B).

    See ``_runner_image_tag``. ``<platform-slug>`` is the most-specific named
    level — ``kubuntu-24.04``, ``ubuntu-24.04``, ``windows-11`` — built by
    [`platforms.platform_slug()`](platforms.py). A different omnetpp version
    means a different image for both toolchains.
    """
    slug = _resolve_platform_slug(
        os_name, os_version, distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
    )
    return _runner_image_tag(slug, toolchain, compiler, compiler_version, omnetpp_version)


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


def render_containerfile(toolchain, os_name, os_version, compiler, compiler_version,
                         *, distro=None, distro_version=None,
                         flavor=None, flavor_version=None,
                         omnetpp_version=None):
    """Render the Containerfile (and, for host toolchain, the entrypoint script)
    for one runner-image combination.

    Returns a dict ``{filename: rendered_content}`` — at minimum
    ``{"Containerfile": "..."}``, plus ``{"opp_ci_entry.sh": "..."}`` for the
    host toolchain. The caller writes each file into the build context.

    For the host template:
      * Current HEAD SHA of opp_env is resolved via 'git ls-remote' and
        baked into the pip-install line. opp_ci and opp_repl are *not*
        baked into the image — the entrypoint clones them at container
        start, so a push to either does not invalidate this image.
      * *omnetpp_version* is required: it's installed via opp_env at
        build time using a nixless workspace.

    For the nix template:
      * Same opp_env pin behaviour, same opp_ci/opp_repl-at-startup model.
      * No omnetpp pre-install: each container run does ``opp_env run
        --install <project>``, which pulls omnetpp + the project + its
        nix_packages (ffmpeg, z3, …) from the Nix store. The Nix store
        is mounted as a named volume so deps are downloaded once per
        worker, not per run.
    """
    import importlib.resources
    from jinja2 import Environment, FileSystemLoader
    import yaml

    template_name = "host" if toolchain == "none" else toolchain

    # Containerfile templates pick package-manager rules off the *distro*
    # (Ubuntu/Fedora/...). Flavors share their parent distro's package
    # base, so we resolve down to the distro for the template context.
    # For Windows/MacOS the runners stay native, never podman.
    base_name = (distro or os_name or "").lower()
    base_version = distro_version or os_version

    podman_dir = importlib.resources.files("opp_ci").joinpath("podman")
    jenv = Environment(loader=FileSystemLoader(str(podman_dir)),
                       keep_trailing_newline=True)
    ctx = {
        "os": base_name,
        "os_version": base_version,
        "compiler": compiler.lower() if compiler else None,
        "compiler_version": compiler_version,
    }
    if template_name == "host":
        if not omnetpp_version:
            raise ValueError("host-toolchain image requires omnetpp_version")
        ctx["omnetpp_version"] = omnetpp_version
        pkgs_path = podman_dir.joinpath("packages.yml")
        with open(pkgs_path) as f:
            pkg_map = yaml.safe_load(f) or {}
        key = f"{ctx['os']}+{ctx['compiler']}-{ctx['compiler_version']}"
        # For gcc the Debian/Ubuntu C++ compiler lives in the separate g++-N
        # package (gcc-N is C-only); g++-N depends on gcc-N, so it installs
        # both — which OMNeT++'s ./configure needs. clang ships clang++ in the
        # same package.
        default_pkg = (f"g++-{ctx['compiler_version']}" if ctx['compiler'] == 'gcc'
                       else f"{ctx['compiler']}-{ctx['compiler_version']}")
        ctx["compiler_package"] = pkg_map.get(key, default_pkg)
        ctx["opp_env_ref"] = _resolve_remote_head(_OPP_ENV_REPO) or "HEAD"
    elif template_name == "nix":
        ctx["opp_env_ref"] = _resolve_remote_head(_OPP_ENV_REPO) or "HEAD"

    files = {"Containerfile": jenv.get_template(f"Containerfile.{template_name}.j2").render(**ctx)}
    if template_name == "host":
        files["opp_ci_entry.sh"] = jenv.get_template("opp_ci_entry.sh.j2").render(**ctx)
    elif template_name == "nix":
        files["opp_env_entry.sh"] = jenv.get_template("opp_env_entry.sh.j2").render(**ctx)
    return files


def _image_exists_locally(tag):
    """True iff 'podman image inspect <tag>' succeeds (image is in the local store)."""
    try:
        result = subprocess.run(
            ["podman", "image", "inspect", tag],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "podman binary not found on PATH — install Podman on this worker, "
            "or untag the worker as podman-capable."
        )
    return result.returncode == 0


def _image_id(tag):
    """The local image ID for *tag*, or None if it isn't present."""
    result = subprocess.run(
        ["podman", "image", "inspect", tag, "--format", "{{.Id}}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _image_label(tag, key):
    """The value of label *key* on image *tag*, or None if absent/unknown."""
    result = subprocess.run(
        ["podman", "image", "inspect", tag, "--format",
         f'{{{{index .Config.Labels "{key}"}}}}'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    # podman renders a missing map key as the Go zero value "<no value>".
    return None if value in ("", "<no value>") else value


def _push_image(tag):
    result = run_external(["podman", "push", tag], label=f"podman push {tag}")
    if result.returncode != 0:
        raise RuntimeError(f"podman push {tag} failed (exit {result.returncode})")


def _label_args(labels, flag):
    """Flatten a {key: value} dict into repeated CLI args.

    flag='--label' → ['--label', 'k=v', ...]   (podman build)
    flag='--change' → ['--change', 'LABEL k=v', ...]  (podman commit)
    """
    args = []
    for k, v in labels.items():
        args += [flag, f"{k}={v}" if flag == "--label" else f"LABEL {k}={v}"]
    return args


def _build_nix_runner_image(final_tag, slug, os_name, os_version,
                            compiler, compiler_version, *, distro, distro_version,
                            flavor, flavor_version, omnetpp_version, push):
    """Bake a per-omnetpp nix runner image via run+commit.

    A Containerfile RUN cannot write into the *runtime* /nix named volume, so
    the workspace (compiled omnetpp) is baked by running ``opp_env install
    omnetpp-<ver>`` in a container with the shared /nix volume mounted, then
    committing it. ``podman commit`` excludes mounted volumes, so the /nix
    store stays in the volume (shared across all images) while only the
    workspace lands in the image layer.

    The omnetpp install is expensive, so it is skipped when *final_tag*
    already exists and was committed from the current base image (tracked via
    the ``org.opp_ci.base-id`` label) — a new opp_env SHA changes the base
    image ID and triggers a rebake.
    """
    if not omnetpp_version:
        raise ValueError("nix runner image requires an omnetpp version to bake")
    base_tag = _nix_base_image_tag(slug)

    # 1. Build/refresh the omnetpp-agnostic base (cheap when layer-cached).
    files = render_containerfile(
        "nix", os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )
    base_labels = {"org.opp_ci.platform": slug, "org.opp_ci.toolchain": "nix"}
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(content)
        result = run_external(
            ["podman", "build", "-t", base_tag, *_label_args(base_labels, "--label"),
             "-f", os.path.join(tmp, "Containerfile"), tmp],
            label=f"podman build {base_tag}", stream=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"podman build {base_tag} failed (exit {result.returncode})")
    base_id = _image_id(base_tag)

    # 2. Skip the omnetpp bake if the final image is already current.
    if _image_exists_locally(final_tag) and _image_label(final_tag, "org.opp_ci.base-id") == base_id:
        _logger.info("%s already baked from current base — skipping omnetpp install", final_tag)
        if push:
            _push_image(final_tag)
        return

    # 3. Provision omnetpp into the workspace against the shared /nix volume,
    #    then commit. uuid (not Date/random) keeps the container name unique.
    cname = f"opp-ci-bake-{uuid.uuid4().hex[:8]}"
    subprocess.run(["podman", "rm", "-f", cname], capture_output=True, text=True)
    try:
        result = run_external(
            ["podman", "run", "--name", cname,
             "-v", f"{_NIX_STORE_VOLUME}:/nix",
             base_tag, "install", f"omnetpp-{omnetpp_version}"],
            label=f"bake omnetpp-{omnetpp_version} into {final_tag}", stream=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"omnetpp-{omnetpp_version} install for {final_tag} failed (exit {result.returncode})"
            )
        commit_labels = _runner_image_labels(slug, "nix", compiler, compiler_version, omnetpp_version)
        commit_labels["org.opp_ci.base-id"] = base_id
        result = run_external(
            ["podman", "commit", *_label_args(commit_labels, "--change"), cname, final_tag],
            label=f"podman commit {final_tag}",
        )
        if result.returncode != 0:
            raise RuntimeError(f"podman commit {final_tag} failed (exit {result.returncode})")
    finally:
        subprocess.run(["podman", "rm", "-f", cname], capture_output=True, text=True)

    if push:
        _push_image(final_tag)


def build_runner_image(tag, toolchain, os_name, os_version, compiler, compiler_version,
                       *, distro=None, distro_version=None,
                       flavor=None, flavor_version=None,
                       omnetpp_version=None, push=False):
    """Build (and optionally push) one opp-ci-runner image.

    *omnetpp_version* is required for both toolchains — it is baked in so the
    container can run opp_repl tests without (re)building OMNeT++ per run:
      - host: the Containerfile runs 'opp_env install --nixless-workspace'.
      - nix:  run+commit bakes the workspace; see ``_build_nix_runner_image``.
    """
    slug = _resolve_platform_slug(
        os_name, os_version, distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
    )
    if toolchain == "nix":
        _build_nix_runner_image(
            tag, slug, os_name, os_version, compiler, compiler_version,
            distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version,
            omnetpp_version=omnetpp_version, push=push,
        )
        return

    files = render_containerfile(
        toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )
    labels = _runner_image_labels(slug, toolchain, compiler, compiler_version, omnetpp_version)
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(content)
        containerfile_path = os.path.join(tmp, "Containerfile")
        result = run_external(
            ["podman", "build", "-t", tag, *_label_args(labels, "--label"),
             "-f", containerfile_path, tmp],
            label=f"podman build {tag}",
            stream=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"podman build {tag} failed (exit {result.returncode})")
    if push:
        _push_image(tag)


def _ensure_runner_image(tag, toolchain, os_name, os_version, compiler, compiler_version,
                         *, distro=None, distro_version=None,
                         flavor=None, flavor_version=None,
                         omnetpp_version=None):
    """Invoke 'podman build' for the image so podman's layer cache picks up
    any changes — new pinned SHAs from an upstream push, edits to the
    Containerfile template, a different compiler package, a different OMNeT++
    version, etc.

    When nothing relevant has changed, every layer hits the cache and the
    call finishes in a couple of seconds. When something has changed, only
    the affected layers (and downstream ones) actually rebuild.
    """
    _logger.info("Ensuring image %s is up to date", tag)
    build_runner_image(
        tag, toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )


def _run_test_in_podman(project, kind, *, toolchain="none", recorder=None, **kwargs):
    """Run a test inside a Podman container.

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
    distro = kwargs.get("distro")
    distro_version = kwargs.get("distro_version")
    flavor = kwargs.get("flavor")
    flavor_version = kwargs.get("flavor_version")
    compiler = kwargs.get("compiler")
    compiler_version = kwargs.get("compiler_version")
    resolved_deps = kwargs.get("resolved_deps") or {}
    omnetpp_version = resolved_deps.get("omnetpp") if isinstance(resolved_deps, dict) else None
    # Every pinned dependency becomes an opp_env '<name>-<version>' token. opp_env
    # accepts these alongside the project on the command line (e.g. 'mm1k
    # omnetpp-6.2.0') and treats them as authoritative — without them it resolves
    # each dependency to its latest version, which would ignore the omnetpp baked
    # into the image and recompile a different one. We pass them to every opp_env
    # install/run below so the run's pins are honoured end to end.
    dep_tokens = ([f"{name}-{ver}" for name, ver in resolved_deps.items()
                   if isinstance(ver, str)]
                  if isinstance(resolved_deps, dict) else [])

    image = _podman_image_tag(
        toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )
    _ensure_runner_image(
        image, toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )

    is_catalog = not opp_file
    worktree_path = None
    scratch_dir = None
    # For catalog runs the worker collapses (project, version) into one name.
    # opp_env needs a *versioned* identifier (e.g. mm1k-latest, inet-4.5) while
    # opp_repl needs the *bare* project name (e.g. mm1k, inet) to match the
    # .opp definition opp_env clones. Split them back out: keep an
    # already-versioned name as-is, otherwise install the "-latest" alias.
    _ver_match = _VERSIONED_NAME_RE.match(project)
    catalog_bare = _ver_match.group(1) if _ver_match else project
    catalog_install_id = project if _ver_match else f"{project}-latest"
    repl_project = catalog_bare if is_catalog else project
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

    # `:Z` relabels the host bind mount for SELinux-enforcing hosts (Fedora);
    # it's a no-op on Ubuntu/Debian. Required for rootless podman to read /work.
    podman_cmd = [
        "podman", "run", "--rm",
        "-v", f"{os.path.abspath(mount_path)}:/work:Z",
        "-w", "/work",
    ]
    if is_catalog:
        # The entrypoint reads this, opp_env-installs each project, and cd's
        # into the first one's install dir.
        podman_cmd += ["-e", f"OPP_CI_INSTALL_PROJECTS={catalog_install_id}"]
    # The image's ENTRYPOINT is the runner binary (opp_env / opp_ci), so we
    # only need to pass its arguments here — repeating the binary name would
    # produce `opp_ci opp_ci ...` and "No such command" from click.
    if toolchain == "nix":
        podman_cmd += ["-v", f"{_NIX_STORE_VOLUME}:/nix"]
        if git_ref:
            podman_cmd += ["-e", f"OPP_ENV_GIT_REF={git_ref}"]
        effective_project, _ = resolve_opp_env_id(project, git_ref, toolchain="nix")
        inner_cmd = COMMAND_MAP.get(kind)
        if inner_cmd is None:
            raise ValueError(f"Unknown test kind: {kind!r}. Supported: {list(COMMAND_MAP.keys())}")
        if mode:
            inner_cmd += f" --mode {mode}"
        # Help opp_repl find the SimulationProject inside the container:
        #   --load @opp  loads the bundled .opp registry (where inet.opp,
        #                omnetpp.opp etc. live and reference the project
        #                root via env vars like INET_ROOT that opp_env sets).
        #   -p <bare>    selects the project; opp_repl knows it as the bare
        #                name (e.g. "inet"), while opp_env's identifier is
        #                versioned (e.g. "inet-4.6.0").
        bare_project = re.sub(r"-[0-9].*$", "", project)
        # opp_repl resolves the project by name from loaded .opp descriptors.
        # @opp is opp_repl's *bundled* registry (omnetpp, inet, …) and does NOT
        # include external catalog projects like mm1k, whose .opp ships in the
        # project repo. opp_env exports <NAME>_ROOT for each installed project,
        # so also load that install dir (its .opp registers the project). It is
        # a no-op for projects already in @opp (install dir has no extra .opp,
        # or an equivalent one). The var is expanded by the container shell,
        # where opp_env has set it — keep it literal in the command string.
        root_var = bare_project.upper().replace("-", "_") + "_ROOT"
        inner_cmd += f' --load @opp --load "${root_var}" -p {bare_project}'
        # --install: have opp_env download + build the project (and its deps,
        # including omnetpp) if not already present in the Nix store.
        # --no-isolated: keep the host PATH visible so opp_build_project and
        # the other opp_repl CLI entries (installed into /opt/opp_ci_venv/bin
        # by the entrypoint) are findable from inside the nix shell.
        # `env -u PYTHONPATH`: nix-shell exports PYTHONPATH pointing at the
        # nix store's pandas/numpy (built for nix's python, e.g. 3.13). The
        # venv's python (e.g. Ubuntu's 3.14) honours PYTHONPATH before its
        # own site-packages and ends up trying to load incompatible packages
        # — strip it so the venv's own pandas/numpy win.
        # Pin every resolved dependency (omnetpp etc.) so opp_env builds the
        # exact versions the run requested rather than the latest available.
        pinned_deps = [t for t in dep_tokens if not t.startswith(f"{bare_project}-")]
        container_args = (["run", "--install", "--no-isolated", effective_project]
                          + pinned_deps
                          + ["-c", f"env -u PYTHONPATH {inner_cmd}"])
    else:
        # Host toolchain: the entrypoint (opp_ci_entry.sh) reads OPP_CI_PIN_DEPS
        # and appends these tokens to both its `opp_env install` (for catalog
        # projects) and `opp_env run`, so the run's pinned dependency versions
        # win over opp_env's latest-version resolution — matching the omnetpp
        # baked into the image instead of recompiling a newer one.
        pinned_deps = [t for t in dep_tokens if not t.startswith(f"{repl_project}-")]
        if pinned_deps:
            podman_cmd += ["-e", f"OPP_CI_PIN_DEPS={' '.join(pinned_deps)}"]
        container_args = ["internal", "run-direct",
                          "--project", repl_project, "--kind", kind]
        if mode:
            container_args += ["--mode", mode]
        if opp_file:
            container_args += ["--opp-file", "/work/" + os.path.basename(opp_file)]
        # Catalog runs deliberately omit --opp-file: opp_repl's _load_workspace
        # auto-discovers any .opp in cwd (the install dir, set by entrypoint),
        # else falls back to a default SimulationProject rooted there.

    podman_cmd.append(image)
    podman_cmd += container_args

    if recorder is not None:
        recorder.begin(Stage.TEST_RUN, command=_format_argv(podman_cmd))
    start = time.time()
    try:
        result = run_external(podman_cmd, label=f"podman:{image}", stream=True,
                              on_output=recorder.output if recorder else None)
        duration = time.time() - start
        if recorder is not None:
            recorder.end(result.returncode)
    finally:
        if worktree_path:
            _remove_git_worktree(worktree_path)
        if scratch_dir:
            import shutil
            shutil.rmtree(scratch_dir, ignore_errors=True)

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    return {
        "result_code": result_code,
        "test_exec_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "details": None,
        "commit_sha": git_ref,
    }


def _run_test_via_opp_env(project, kind, recorder=None, **kwargs):
    """Run a test via opp_env subprocess (Nix environment on the host).

    Compilation and the test run as two opp_env invocations — a
    ``project.build`` stage (``opp_build_project``) then a ``test.run`` stage
    (the test command with ``--no-build``) — so each is captured separately
    and a compile failure is attributed to the build stage instead of being
    buried in the test output. Both share the per-coordinate workspace, so the
    build's artifacts are reused by the test (no rebuild).
    """
    git_ref = kwargs.get("git_ref")
    mode = kwargs.get("mode")

    test_command = COMMAND_MAP.get(kind)
    if test_command is None:
        raise ValueError(f"Unknown test kind: {kind!r}. Supported: {list(COMMAND_MAP.keys())}")

    mode_suffix = f" --mode {mode}" if mode else ""
    build_inner = "opp_build_project" + mode_suffix
    test_inner = test_command + mode_suffix + " --no-build"

    env = os.environ.copy()
    effective_project, effective_ref = resolve_opp_env_id(project, git_ref, toolchain="nix")
    if effective_ref:
        env["OPP_ENV_GIT_REF"] = effective_ref

    # Resolve the same per-coordinate workspace the install step created and
    # run from it (opp_env auto-detects the workspace from cwd). The axes must
    # match install_project's exactly, or run would land in a different dir and
    # rebuild omnetpp. toolchain isn't in kwargs here (run_test consumes it),
    # but this path is always nix.
    ws = _opp_env_workspace(
        project=project, resolved_deps=kwargs.get("resolved_deps"),
        toolchain="nix", compiler=kwargs.get("compiler"),
        compiler_version=kwargs.get("compiler_version"), git_ref=git_ref,
    )

    def _opp_env_run(inner):
        return run_external(
            ["opp_env", "run", effective_project, "-c", inner],
            label=f"opp_env:{effective_project}", env=env, cwd=ws,
            stream=True, on_output=recorder.output if recorder else None)

    with _workspace_lock(ws):
        # ── project.build ─────────────────────────────────────────────
        if recorder is not None:
            recorder.begin(Stage.PROJECT_BUILD,
                           command=f"opp_env run {effective_project} -c {build_inner!r}")
        build = _opp_env_run(build_inner)
        if recorder is not None:
            recorder.end(build.returncode)
        if build.returncode != 0:
            if recorder is not None:
                recorder.skip(Stage.TEST_RUN, reason="skipped: build failed")
            return {
                "result_code": "FAIL",
                "test_exec_seconds": 0.0,
                "stdout": build.stdout,
                "stderr": build.stderr,
                "details": None,
                "commit_sha": None,
            }
        # ── test.run ──────────────────────────────────────────────────
        if recorder is not None:
            recorder.begin(Stage.TEST_RUN,
                           command=f"opp_env run {effective_project} -c {test_inner!r}")
        start = time.time()
        result = _opp_env_run(test_inner)
        duration = time.time() - start
        if recorder is not None:
            recorder.end(result.returncode)

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    return {
        "result_code": result_code,
        "test_exec_seconds": duration,
        "stdout": (build.stdout or "") + (result.stdout or ""),
        "stderr": (build.stderr or "") + (result.stderr or ""),
        "details": None,
        "commit_sha": None,
    }


def _run_test_direct(project, kind, *, opp_file=None, git_ref=None, mode=None,
                     recorder=None, **_unused):
    """Run a test by calling opp_repl functions directly (no subprocess).

    When *git_ref* is set, an isolated git worktree is created for that
    commit and removed after the test completes.

    Extra kwargs (e.g. os, compiler) are accepted-and-ignored so this can
    sit downstream of the run_test dispatcher. The whole run is captured as a
    single ``test.run`` stage when a ``recorder`` is given (this in-process
    path doesn't split build out yet — that's the opp_env/podman paths).
    """
    test_functions = _get_test_functions()
    func = test_functions.get(kind)
    if func is None:
        raise ValueError(f"Unknown test kind: {kind!r}. Supported: {list(test_functions.keys())}")

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

    _logger.info("Running %s test for %s (direct mode)", kind, project)
    out_cb = (lambda text: recorder.output("out", text)) if recorder else None
    err_cb = (lambda text: recorder.output("err", text)) if recorder else None
    stdout_buf = _CallbackStringIO(out_cb) if out_cb else io.StringIO()
    stderr_buf = _CallbackStringIO(err_cb) if err_cb else io.StringIO()
    if recorder is not None:
        recorder.begin(Stage.TEST_RUN, command=f"{func.__name__} (direct)")
    start = time.time()
    try:
        call_kwargs = {"simulation_project": simulation_project, "build": "task", "build_mode": "task"}
        if mode:
            call_kwargs["mode"] = mode
        if kind == "opp":
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
        _logger.error("Test %s raised exception: %s", kind, e)
        if recorder is not None:
            recorder.output("err", repr(e))
            recorder.end(1, status="failed")
        return {
            "result_code": "ERROR",
            "test_exec_seconds": duration,
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue() + "\n" + repr(e),
            "details": None,
            "commit_sha": git_ref,
        }
    finally:
        if worktree_path:
            _remove_git_worktree(worktree_path)

    if recorder is not None:
        recorder.end(0 if result_code == "PASS" else 1)
    commit_sha = git_ref or resolve_commit_sha(project, opp_file=opp_file)
    _logger.info("Test finished: %s (%.1fs)", result_code, duration)
    return {
        "result_code": result_code,
        "test_exec_seconds": duration,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "details": details,
        "commit_sha": commit_sha,
    }
