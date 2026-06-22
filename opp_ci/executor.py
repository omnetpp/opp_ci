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

# Marker the container entry scripts (opp_ci/podman/*.j2) prefix onto each
# meaningful command via their run_cmd helper; _run_podman_staged strips it and
# records that line as the "cmd" stream so the UI colours it as a command.
_CMD_MARKER = "@@oppci:cmd@@ "


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
    Resolve the opp_env project name for testing a specific git ref.

    A git_ref is realized by switching to the project's '-git' variant and
    baking the ref into opp_env's ``name@ref`` syntax (e.g.
    ``inet-git@<commit>``), which does a full-clone ``git checkout`` of that
    ref. This is the *same* per-project mechanism dependency pins use
    (``omnetpp-git@<sha>``), so source and deps share one code path and
    multiple git projects can coexist in one workspace — unlike the former
    single global ``OPP_ENV_GIT_REF`` env var (which opp_env never read).

    Returns (effective_project, effective_ref); ``effective_ref`` is always
    None now — the ref rides inside the project token — but the second slot is
    kept so existing call sites stay tuple-compatible.
    """
    if not git_ref:
        return project, None
    base = project if project.endswith("-git") else f"{project}-git"
    return f"{base}@{git_ref}", None


# A name already carries an explicit version when it ends in a numeric version
# (e.g. inet-4.5), or the pseudo-versions "latest" / "git".
_VERSIONED_NAME_RE = re.compile(r"^(.*?)-(\d.*|latest|git)$")


def resolve_opp_env_id(project, git_ref=None, *, toolchain="nix"):
    """Resolve the *versioned* opp_env project identifier for install/run.

    opp_env rejects a bare catalog name like 'mm1k' as ambiguous ("Which
    version of 'mm1k' do you mean?"); it needs a versioned identifier. The
    rules mirror the podman entrypoint's catalog-name split:

      - a specific git_ref → the '-git' variant with the ref baked in
        (``inet-git@<commit>``), used verbatim
      - an already-versioned name (inet-4.5, mm1k-latest, foo-git) → kept as-is
      - a bare name → the '-latest' alias

    Returns (effective_project, effective_ref); ``effective_ref`` is always
    None (the ref rides inside the project token).
    """
    effective_project, _ = resolve_git_project(project, git_ref, toolchain=toolchain)
    # A baked git ref (name-git@ref) or an already-versioned name is used as-is;
    # only a bare catalog name needs the -latest alias.
    if "@" in effective_project or _VERSIONED_NAME_RE.match(effective_project):
        return effective_project, None
    return f"{effective_project}-latest", None


def opp_env_project_id(project, version):
    """Combine a project name and an opp_env *version field* into the full
    opp_env project id.

    opp_env's per-version ``version`` field is just the suffix (e.g. ``"git"``
    for mm1k, ``"4.5"`` for inet), while its install/run id is ``name-version``
    (``mm1k-git``, ``inet-4.5``). So combine them — unless `version` is already a
    full id (``mm1k-git``) or absent (then the bare project name, which
    ``resolve_opp_env_id`` later maps to the ``-latest`` alias). This is the fix
    for "opp_env install … git-latest: Unknown project 'git'".
    """
    if not version:
        return project
    return version if version.startswith(f"{project}-") else f"{project}-{version}"


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

    Runs for the host paths (``isolation=none``); podman installs inside the
    container's entrypoint instead, so that case is a no-op here.
      - toolchain=nix  → opp_env installs into a Nix-isolated workspace.
      - toolchain=none → opp_env installs into a ``--nixless-workspace``: the
        pinned omnetpp (and any other deps) are built with the *host* toolchain,
        the same mechanism the podman host image uses. Nothing is assumed to be
        pre-installed on the host.

    The remaining keyword args (``resolved_deps``, ``compiler``,
    ``compiler_version``) plus *project* and *git_ref* form the build
    coordinate that picks the per-coordinate workspace; they must match the
    axes the run step passes to :py:func:`_opp_env_workspace` so install and
    run land in the *same* directory (omnetpp built once, reused by the run).

    The pinned omnetpp version comes from *resolved_deps* (the Test
    coordinate), never from anything found on the host.

    When ``recorder`` is given and the install actually runs, it is captured as
    a ``deps.install`` stage; on the podman no-op path no stage is added.
    """
    if isolation != "none" or toolchain not in ("nix", "none"):
        _logger.info("Skipping install (isolation=%s, toolchain=%s)", isolation, toolchain)
        return

    effective_project, _ = resolve_opp_env_id(project, git_ref, toolchain=toolchain)
    ws = _opp_env_workspace(
        project=project, resolved_deps=resolved_deps, toolchain=toolchain,
        compiler=compiler, compiler_version=compiler_version, git_ref=git_ref,
    )
    _gc_workspaces()
    # --init marks the workspace on first use (same flag the host Containerfile
    # uses); it is a no-op once the workspace exists, so reuse is idempotent.
    # toolchain=none → --nixless-workspace (host toolchain). The pinned deps are
    # passed as positional projects so opp_env builds those exact versions
    # rather than resolving each to its latest.
    pins = _opp_env_pin_args(resolved_deps)
    argv = _opp_env_cmd() + ["install", "--init"]
    if toolchain == "none":
        argv.append("--nixless-workspace")
    argv += pins + [effective_project]
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
      isolation=none, tc=nix    → _run_test_via_opp_env  (opp_env on host, Nix)
      isolation=none, tc=none   → _run_test_via_opp_env  (opp_env on host,
                                  --nixless-workspace: host toolchain, pinned
                                  omnetpp from the coordinate's resolved_deps)

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
    # Both host paths provision the pinned omnetpp via opp_env; toolchain=none
    # uses a --nixless-workspace (host toolchain). The legacy in-process
    # _run_test_direct had no version-aware provider and is no longer dispatched.
    return _run_test_via_opp_env(project, kind, recorder=recorder,
                                 toolchain=toolchain, **kwargs)


def _opp_env_cmd():
    """The argv prefix for invoking opp_env on the host-nix path.

    Thin wrapper over :func:`config.opp_env_argv` (shared with the coordinator's
    dependency resolution in opp_ci/dependency.py). Read at call time so an
    env-file override applied after import still takes effect.
    """
    from opp_ci import config as cfg
    return cfg.opp_env_argv()


def _opp_env_pin_args(resolved_deps):
    """Turn a resolved_deps mapping into opp_env positional project pins.

    ``{"omnetpp": "6.4.0"}`` → ``["omnetpp-6.4.0"]``; a git-ref dep
    ``{"omnetpp": {"git": "omnetpp-6.x", "commit": "<sha>"}}`` →
    ``["omnetpp-git@<sha>"]`` (the ``-git`` variant checked out at the pinned
    commit). Passing these as positional projects to ``opp_env install``/``run``
    makes opp_env build the *exact* pinned versions instead of resolving each
    dependency to its latest (mirrors ``$OPP_CI_PIN_DEPS`` in the podman host
    entrypoint). Sorted for a stable command line. Deps with no version are
    skipped.
    """
    from opp_ci.dependency import dep_build_token
    deps = resolved_deps if isinstance(resolved_deps, dict) else {}
    return [dep_build_token(name, ver) for name, ver in sorted(deps.items()) if ver]


def _strip_git_ref(token):
    """Drop a trailing ``@<ref>`` from an opp_env project token.

    ``opp_env install`` accepts a ``name@<ref>`` token (it clones and checks out
    that git ref), but ``opp_env run`` REJECTS it ("Git branch may only be
    specified when the project is installed") unless ``--install`` is also given.
    The bare-metal run path installs first (a separate step), so the run only
    needs the versioned id — strip the ``@<ref>`` suffix. Tokens without an
    ``@`` pass through unchanged."""
    return token.split("@", 1)[0]


def _project_install_dir(ws, project):
    """Return the project's opp_env install dir under *ws*, or *ws* itself.

    opp_env installs each project into ``<ws>/<name>-<version>`` (e.g.
    ``mm1k-git``, ``omnetpp-6.4.0``). opp_repl discovers the simulation project
    from the current directory, so the build/test command must run *inside*
    this dir — running from the workspace root yields "No enclosing simulation
    project is found". Strip any ``-latest``/``-git``/``-<version>`` suffix to a
    bare prefix and glob; the bare prefix scopes to this project (omnetpp and
    other deps have different prefixes). Falls back to *ws* when nothing
    matches (e.g. install hasn't run yet), preserving prior behaviour.
    """
    import glob

    bare = re.sub(r"-(latest|git|\d.*)$", "", project)
    matches = sorted(
        d for d in glob.glob(os.path.join(ws, bare + "*")) if os.path.isdir(d))
    return matches[0] if matches else ws


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
    from opp_ci.db.models import dep_identity_token

    deps = resolved_deps if isinstance(resolved_deps, dict) else {}
    # Canonical, order-independent view of the coordinate: sorting keys and
    # normalising None→"" means dict iteration order or a missing axis can
    # never shift the hash between the install and run steps. Each dep value is
    # reduced to its identity token (release version, or "git:<sha>"), so two
    # git commits of the same dep get distinct workspaces and a dict's key order
    # can't perturb the hash.
    coordinate = {
        "project": project or "",
        "toolchain": toolchain or "",
        "compiler": compiler or "",
        "compiler_version": str(compiler_version or ""),
        "git_ref": git_ref or "",
        "deps": {str(k): dep_identity_token(v) for k, v in sorted(deps.items())},
    }
    canon = json.dumps(coordinate, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha1(canon.encode()).hexdigest()[:8]

    omnetpp = deps.get("omnetpp")
    omnetpp_label = dep_identity_token(omnetpp).replace(":", "") if omnetpp else ""
    raw_prefix = "-".join(filter(None, [
        project or "proj",
        f"omnetpp{omnetpp_label}" if omnetpp else "omnetpp",
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
    # The lock lives *beside* the workspace dir, not inside it: `opp_env
    # install --init` refuses to initialise a non-empty directory, so the dir
    # must be empty when opp_env first runs. A sibling file also can't be
    # mistaken for a workspace by _gc_workspaces (it filters to os.path.isdir).
    return ws.rstrip("/") + ".opp_ci.lock"


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
        evicted = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _logger.debug("Skipping GC of in-use workspace %s", ws)
                continue
            shutil.rmtree(ws, ignore_errors=True)
            _logger.info("Evicted LRU opp_env workspace %s", ws)
            evicted = True
        finally:
            os.close(fd)
        # Only after a successful eviction: drop the now-orphan sibling lock
        # file. Never unlink it on the skip path — another job holds it.
        if evicted:
            try:
                os.unlink(_workspace_lock_path(ws))
            except OSError:
                pass


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


def _create_git_worktree(project_dir, git_ref, on_output=None):
    """Create a detached git worktree for *git_ref* in a temp dir and return its path."""
    target = os.path.join(tempfile.gettempdir(), f"opp-ci-worktree-{uuid.uuid4().hex[:8]}")
    run_external(
        ["git", "fetch", "origin"], label="git fetch", cwd=project_dir, timeout=120,
        stream=True, on_output=on_output,
    )
    result = run_external(
        ["git", "worktree", "add", "--detach", target, git_ref],
        label=f"git worktree add {git_ref}", cwd=project_dir,
        stream=True, on_output=on_output,
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


# Repo URLs / refs live in config (single source of truth, shared with
# service.py). render_containerfile reads them via a lazy `cfg` import.
_OPP_CI_REPO = "https://github.com/omnetpp/opp_ci.git"


def render_containerfile(toolchain, os_name, os_version, compiler, compiler_version,
                         *, distro=None, distro_version=None,
                         flavor=None, flavor_version=None,
                         omnetpp_version=None, omnetpp_build=None):
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

    from opp_ci import config as cfg

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
        # opp_repl is cloned + pip-installed by the entry script and (host
        # image) baked for its [all] deps — both at the `opp_ci` branch.
        "opp_repl_repo": cfg.OPP_REPL_REPO,
        "opp_repl_ref": cfg.OPP_REPL_REF,
    }
    if template_name == "host":
        if not omnetpp_version:
            raise ValueError("host-toolchain image requires omnetpp_version")
        ctx["omnetpp_version"] = omnetpp_version
        # The opp_env install token: a release (omnetpp-6.4.0) or a git ref
        # (omnetpp-git@<commit>). Defaults from the slug for a release dep.
        ctx["omnetpp_install"] = omnetpp_build or f"omnetpp-{omnetpp_version}"
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
        # Bake the SHA at the tip of opp_env's `opp_ci` branch (not its default
        # branch HEAD), so the image tracks the same opp_env as the host paths.
        ctx["opp_env_ref"] = (_resolve_remote_head(cfg.OPP_ENV_REPO, cfg.OPP_ENV_REF)
                              or cfg.OPP_ENV_REF)
    elif template_name == "nix":
        ctx["opp_env_ref"] = (_resolve_remote_head(cfg.OPP_ENV_REPO, cfg.OPP_ENV_REF)
                              or cfg.OPP_ENV_REF)

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
                            flavor, flavor_version, omnetpp_version, push,
                            omnetpp_build=None, on_output=None):
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
            label=f"podman build {base_tag}", stream=True, on_output=on_output,
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
        install_token = omnetpp_build or f"omnetpp-{omnetpp_version}"
        result = run_external(
            ["podman", "run", "--name", cname,
             "-v", f"{_NIX_STORE_VOLUME}:/nix",
             base_tag, "install", install_token],
            label=f"bake {install_token} into {final_tag}", stream=True,
            on_output=on_output,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{install_token} install for {final_tag} failed (exit {result.returncode})"
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
                       omnetpp_version=None, omnetpp_build=None, push=False,
                       on_output=None):
    """Build (and optionally push) one opp-ci-runner image.

    *omnetpp_version* is required for both toolchains — it is baked in so the
    container can run opp_repl tests without (re)building OMNeT++ per run:
      - host: the Containerfile runs 'opp_env install --nixless-workspace'.
      - nix:  run+commit bakes the workspace; see ``_build_nix_runner_image``.

    *omnetpp_version* is the tag-safe identity slug (e.g. ``6.4.0`` or
    ``git-<short8>``); *omnetpp_build* is the opp_env install token actually
    baked (``omnetpp-6.4.0`` or ``omnetpp-git@<commit>``), defaulting from the
    slug for a release dep.
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
            omnetpp_version=omnetpp_version, omnetpp_build=omnetpp_build,
            push=push, on_output=on_output,
        )
        return

    files = render_containerfile(
        toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version, omnetpp_build=omnetpp_build,
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
            stream=True, on_output=on_output,
        )
        if result.returncode != 0:
            raise RuntimeError(f"podman build {tag} failed (exit {result.returncode})")
    if push:
        _push_image(tag)


def _ensure_runner_image(tag, toolchain, os_name, os_version, compiler, compiler_version,
                         *, distro=None, distro_version=None,
                         flavor=None, flavor_version=None,
                         omnetpp_version=None, omnetpp_build=None, on_output=None):
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
        omnetpp_version=omnetpp_version, omnetpp_build=omnetpp_build,
        on_output=on_output,
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
    from opp_ci.dependency import dep_build_token, dep_tag_slug
    resolved_deps = kwargs.get("resolved_deps") or {}
    deps_map = resolved_deps if isinstance(resolved_deps, dict) else {}
    omnetpp_dep = deps_map.get("omnetpp")
    # The image is content-addressed by a tag-safe omnetpp *slug* (a release
    # version, or git-<short8> for a git ref); the per-commit omnetpp itself is
    # baked via the opp_env *build token* (omnetpp-6.4.0 or omnetpp-git@<sha>).
    omnetpp_version = dep_tag_slug(omnetpp_dep) if omnetpp_dep else None
    omnetpp_build = dep_build_token("omnetpp", omnetpp_dep) if omnetpp_dep else None
    # Every pinned dependency becomes an opp_env '<name>-<version>' token. opp_env
    # accepts these alongside the project on the command line (e.g. 'mm1k
    # omnetpp-6.2.0') and treats them as authoritative — without them it resolves
    # each dependency to its latest version, which would ignore the omnetpp baked
    # into the image and recompile a different one. We pass them to every opp_env
    # install/run below so the run's pins are honoured end to end.
    dep_tokens = [dep_build_token(name, ver) for name, ver in deps_map.items()
                  if ver]

    image = _podman_image_tag(
        toolchain, os_name, os_version, compiler, compiler_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        omnetpp_version=omnetpp_version,
    )
    # container.prepare: build/refresh (and, for nix, bake) the runner image.
    # Cheap and near-silent when everything is layer-cached; the slow first
    # build streams into this stage. A build failure fails the stage and
    # propagates (the worker reports ERROR).
    if recorder is not None:
        recorder.begin(Stage.CONTAINER_PREPARE, command=f"ensure runner image {image}")
    try:
        _ensure_runner_image(
            image, toolchain, os_name, os_version, compiler, compiler_version,
            distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version,
            omnetpp_version=omnetpp_version, omnetpp_build=omnetpp_build,
            on_output=recorder.output if recorder else None,
        )
    except Exception:
        if recorder is not None:
            recorder.end(1, status="failed")
        raise
    if recorder is not None:
        recorder.end(0)

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
            # checkout: detached worktree at the requested ref (host side, then
            # bind-mounted). Captured as its own stage so a bad ref is obvious.
            if recorder is not None:
                recorder.begin(Stage.CHECKOUT, command=f"git worktree add {git_ref}")
            try:
                worktree_path = _create_git_worktree(
                    project_dir, git_ref,
                    on_output=recorder.output if recorder else None)
            except Exception:
                if recorder is not None:
                    recorder.end(1, status="failed")
                raise
            if recorder is not None:
                recorder.end(0)
            mount_path = worktree_path
        else:
            mount_path = project_dir

    # `:Z` relabels the host bind mount for SELinux-enforcing hosts (Fedora);
    # it's a no-op on Ubuntu/Debian. Required for rootless podman to read /work.
    run_flags = [
        "-v", f"{os.path.abspath(mount_path)}:/work:Z",
        "-w", "/work",
    ]
    if is_catalog:
        # The entrypoint reads this, opp_env-installs each project, and cd's
        # into the first one's install dir.
        run_flags += ["-e", f"OPP_CI_INSTALL_PROJECTS={catalog_install_id}"]
    # opp_repl's --result-file writes result.to_dict() to this in-container path
    # (a host dir is bind-mounted there at the tail) so we can read it into
    # `details` afterwards — see plan §2.7. Build-only runs write nothing there,
    # so details stays None.
    container_result = "/opp_ci_result/result.json"
    if toolchain == "nix":
        run_flags += ["-v", f"{_NIX_STORE_VOLUME}:/nix"]
        # The source ref is realized by the bind-mounted host worktree (see
        # _create_git_worktree above), not by opp_env — so the opp_env id here
        # stays ref-free. A ref still selects the '-git' variant (its
        # git-tracking dependency set), matching the prior behaviour.
        if git_ref:
            effective_project = project if project.endswith("-git") else f"{project}-git"
        else:
            effective_project, _ = resolve_opp_env_id(project, None, toolchain="nix")
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

        # Split compilation from the test: opp_build_project (project.build)
        # then the test command with --no-build (test.run), as two opp_env
        # invocations over the same persistent /opt/opp_env_workspace, so the
        # build's artifacts are reused. opp_build_project takes the same
        # --load/-p/--mode flags as the test commands. `inner_cmd` already has
        # the test command + mode + the --load/-p suffix; reuse that suffix for
        # the build command. (omnetpp's own compile rides inside --install on
        # the build stage — see the taxonomy note in the plan.)
        repl_suffix = inner_cmd[len(COMMAND_MAP[kind]):]   # " --mode … --load … -p …"
        build_inner = "opp_build_project" + repl_suffix
        # --result-file captures the structured result into `details`; it is
        # plumbing, so it stays out of the curated stage display below.
        test_inner = inner_cmd + " --no-build" + f" --result-file {container_result}"

        def _oe_args(inner):
            return (["run", "--install", "--no-isolated", effective_project]
                    + pinned_deps + ["-c", f"env -u PYTHONPATH {inner}"])

        # Three groups: deps.install (opp_env --install resolves/installs the
        # project + its deps; omnetpp is pre-baked into the image — see
        # container.prepare — so this is mostly the project install/clone),
        # project.build (opp_build_project), and test.run (the test with
        # --no-build). The build/test commands are byte-identical to the
        # combined form; deps.install just front-loads the (idempotent)
        # --install so it is its own stage and an install failure is
        # attributed there, skipping build + test.
        # Each stage carries a third element: the *bare* command to show in the
        # stage view, stripped of the opp_env / `-c env -u PYTHONPATH …` /
        # entry-script / podman-exec plumbing that the executed argv needs. The
        # plumbing still goes to the worker journal (run_external logs the full
        # argv); the UI just shows what actually ran inside.
        mode_suffix = f" --mode {mode}" if mode else ""
        run_stages = [
            (Stage.DEPS_INSTALL, _oe_args("true"), f"opp_env install {effective_project}"),
            (Stage.PROJECT_BUILD, _oe_args(build_inner), "opp_build_project" + mode_suffix),
            (Stage.TEST_RUN, _oe_args(test_inner), COMMAND_MAP[kind] + mode_suffix + " --no-build"),
        ]
        entry_script = "/opt/opp_env_entry.sh"
    else:
        # Host toolchain: the entrypoint (opp_ci_entry.sh) reads OPP_CI_PIN_DEPS
        # and appends these tokens to both its `opp_env install` (for catalog
        # projects) and `opp_env run`, so the run's pinned dependency versions
        # win over opp_env's latest-version resolution — matching the omnetpp
        # baked into the image instead of recompiling a newer one.
        pinned_deps = [t for t in dep_tokens if not t.startswith(f"{repl_project}-")]
        if pinned_deps:
            run_flags += ["-e", f"OPP_CI_PIN_DEPS={' '.join(pinned_deps)}"]
        build_only = kind == "build"
        test_command = COMMAND_MAP.get(kind)
        if test_command is None:
            raise ValueError(f"Unknown test kind: {kind!r}. Supported: {list(COMMAND_MAP.keys())}")
        # Drive opp_repl's console scripts directly (no opp_ci inside the
        # container — see plan §2.6), the same way the nix branch does. The
        # entry script wraps the stage argv in `opp_env run -w <ws> <pins> -c`.
        # Project-discovery flags mirror the nix branch:
        #   --load @opp   bundled registry (omnetpp, inet, …)
        #   --load <src>  the project's own .opp — its opp_env install-dir
        #                 $<NAME>_ROOT for a catalog project (expanded by
        #                 opp_env's -c shell), or the bind-mounted /work/<file>
        #                 for a SimulationProject
        #   -p <name>     select the project
        repl_flags = ["--load", "@opp"]
        if is_catalog:
            root_var = repl_project.upper().replace("-", "_") + "_ROOT"
            repl_flags += ["--load", f"${root_var}"]
        else:
            repl_flags += ["--load", "/work/" + os.path.basename(opp_file)]
        repl_flags += ["-p", repl_project]
        mode_flags = ["--mode", mode] if mode else []
        mode_suffix = f" --mode {mode}" if mode else ""
        # Split build from test as two execs over the same container (shared
        # source — bind-mounted /work or the catalog install dir — persists, so
        # the test reuses the build's artifacts). opp_build_project for the
        # build; the kind's test command with --no-build for the test. For
        # kind=build the build is the whole job, so there is no test stage
        # (opp_build_project has no --no-build flag).
        build_args = ["opp_build_project"] + mode_flags + repl_flags
        # --result-file captures the structured result into `details` (plumbing,
        # kept out of the curated stage display).
        test_args = ([test_command] + mode_flags + repl_flags
                     + ["--no-build", "--result-file", container_result])
        run_stages = []
        if is_catalog:
            # Install the catalog project(s) once, as their own stage
            # (entry script --do-install), instead of re-running the install
            # loop on every build/test exec. A bind-mounted SimulationProject
            # needs no install stage — its source is already at /work.
            run_stages.append(
                (Stage.DEPS_INSTALL, ["--do-install"], f"opp_env install {repl_project}"))
        run_stages.append(
            (Stage.PROJECT_BUILD, build_args, "opp_build_project" + mode_suffix))
        if not build_only:
            run_stages.append(
                (Stage.TEST_RUN, test_args, test_command + mode_suffix + " --no-build"))
        entry_script = "/opt/opp_ci_entry.sh"

    # Create + bind-mount the host result dir now (after the branch logic, so an
    # unknown-kind error can't leak it); read its result.json into `details`,
    # then remove it.
    result_dir = tempfile.mkdtemp(prefix="opp-ci-result-")
    run_flags += ["-v", f"{result_dir}:/opp_ci_result:Z"]
    try:
        return _run_podman_staged(
            image=image, run_stages=run_stages, entry_script=entry_script,
            run_flags=run_flags, recorder=recorder, git_ref=git_ref,
            worktree_path=worktree_path, scratch_dir=scratch_dir,
            result_file=os.path.join(result_dir, "result.json"))
    finally:
        import shutil
        shutil.rmtree(result_dir, ignore_errors=True)


def _run_podman_staged(*, image, run_stages, entry_script, run_flags,
                       recorder, git_ref, worktree_path, scratch_dir,
                       result_file=None):
    """Run a podman job as a long-lived container driven stage by stage
    (option b of plan/pending/staged-execution-capture.md).

    Instead of one `podman run <image> <args>`, start the container detached
    (entrypoint overridden to idle) and drive it with separate `podman exec`s:
    a runner.bootstrap stage (clone opp_repl + pip install via the entry
    script's --bootstrap-only), then each (stage_name, args) in *run_stages*
    via the entry script --skip-bootstrap. A failed stage aborts the rest
    (build fails → test skipped). Each exec is captured like any host command.
    The container is always removed in the finally — even if a stage fails or
    raises — so a crash can't leak containers.

    *result_file* (when given) is the host-side path of the opp_repl
    ``--result-file`` JSON written by the test stage (via a bind-mounted
    results dir); it is read into the returned ``details``. Needs a real podman
    host to validate.
    """
    container = f"opp_ci_run_{uuid.uuid4().hex[:12]}"
    run_d = (["podman", "run", "-d", "--name", container]
             + run_flags + ["--entrypoint", "sleep", image, "infinity"])

    def _on_output(stream, text):
        # The entry script tags meaningful commands with a marker (run_cmd);
        # surface those as the "cmd" stream (rendered as a coloured command
        # line), everything else as the stream it came from.
        if text.startswith(_CMD_MARKER):
            recorder.output("cmd", text[len(_CMD_MARKER):])
        else:
            recorder.output(stream, text)

    def _exec(args, label):
        return run_external(["podman", "exec", container] + args, label=label,
                            stream=True,
                            on_output=_on_output if recorder is not None else None)

    out_parts, err_parts = [], []
    result = None
    start = time.time()
    try:
        up = run_external(run_d, label=f"podman run -d {image}")
        if up.returncode != 0:
            raise RuntimeError(
                f"podman run -d for {image} failed (exit {up.returncode}): "
                f"{(up.stderr or '').strip()}")

        # ── runner.bootstrap ──────────────────────────────────────────
        if recorder is not None:
            # No headline: bootstrap runs several real sub-commands (git clone,
            # pip install) that the entry script echoes via run_cmd as cmd
            # lines — a "--bootstrap-only" headline would just be noise above them.
            recorder.begin(Stage.RUNNER_BOOTSTRAP, command=None)
        boot = _exec([entry_script, "--bootstrap-only"],
                     label=f"podman:{container}:bootstrap")
        if recorder is not None:
            recorder.end(boot.returncode)
        out_parts.append(boot.stdout or "")
        err_parts.append(boot.stderr or "")

        if boot.returncode != 0:
            if recorder is not None:
                for stage_name, *_rest in run_stages:
                    recorder.skip(stage_name, reason="skipped: bootstrap failed")
            result = boot
        else:
            ran = 0
            for stage_name, args, _display in run_stages:
                if recorder is not None:
                    # No curated headline — the stage title already says what
                    # this is, and the real commands show in green via the entry
                    # script's run_cmd markers (cmd stream).
                    recorder.begin(stage_name, command=None)
                result = _exec([entry_script, "--skip-bootstrap"] + args,
                               label=f"podman:{image}:{stage_name}")
                if recorder is not None:
                    recorder.end(result.returncode)
                out_parts.append(result.stdout or "")
                err_parts.append(result.stderr or "")
                ran += 1
                if result.returncode != 0:
                    # Abort the rest (e.g. build fails → skip test).
                    if recorder is not None:
                        for skip_name, *_a in run_stages[ran:]:
                            recorder.skip(skip_name, reason="skipped: previous stage failed")
                    break
    finally:
        # cleanup: remove the container (and host worktree/scratch). Its own
        # best-effort stage; a failed remove must not mask the run's result.
        if recorder is not None:
            recorder.begin(Stage.CLEANUP, command=f"podman rm -f {container}")
        rm = run_external(["podman", "rm", "-f", container],
                          label=f"podman rm {container}",
                          stream=True, on_output=recorder.output if recorder else None)
        if worktree_path:
            _remove_git_worktree(worktree_path)
        if scratch_dir:
            import shutil
            shutil.rmtree(scratch_dir, ignore_errors=True)
        if recorder is not None:
            recorder.end(rm.returncode)
    duration = time.time() - start

    result_code = "PASS" if (result is not None and result.returncode == 0) else "FAIL"
    return {
        "result_code": result_code,
        "test_exec_seconds": duration,
        "stdout": "".join(out_parts),
        "stderr": "".join(err_parts),
        "details": _read_result_file(result_file) if result_file else None,
        "commit_sha": git_ref,
    }


def _read_result_file(path):
    """Read opp_repl's ``--result-file`` JSON (``result.to_dict()``) for the
    out-of-process run paths (host-nix subprocess + podman), so it lands in
    ``TestRun.details``. Missing/corrupt → None (the stage exit code still
    drives PASS/FAIL); details only enriches."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _run_test_via_opp_env(project, kind, recorder=None, toolchain="nix", **kwargs):
    """Run a test via opp_env subprocess (opp_env environment on the host).

    Serves both host toolchains: ``nix`` (Nix-isolated workspace) and ``none``
    (the ``--nixless-workspace`` the install step created, where opp_env built
    the pinned omnetpp with the host toolchain). The only difference here is the
    workspace coordinate; the run names the same pinned deps either way.

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
    # The source ref (if any) rides inside effective_project as the opp_env
    # `name-git@<ref>` token — no separate env var to set.
    effective_project, _ = resolve_opp_env_id(project, git_ref, toolchain=toolchain)

    # Resolve the same per-coordinate workspace the install step created and
    # run from it (opp_env auto-detects the workspace from cwd). The axes —
    # including toolchain — must match install_project's exactly, or run would
    # land in a different dir and rebuild omnetpp.
    ws = _opp_env_workspace(
        project=project, resolved_deps=kwargs.get("resolved_deps"),
        toolchain=toolchain, compiler=kwargs.get("compiler"),
        compiler_version=kwargs.get("compiler_version"), git_ref=git_ref,
    )

    # Name the pinned deps (e.g. omnetpp-6.4.0) alongside the project so opp_env
    # sets up the matching environment instead of resolving to latest.
    pins = _opp_env_pin_args(kwargs.get("resolved_deps"))

    # Run inside the project's install dir so opp_repl can discover the project
    # from cwd; pass -w explicitly so workspace detection doesn't depend on it.
    run_cwd = _project_install_dir(ws, project)

    # The install step already checked out any git refs; `opp_env run` (no
    # --install) rejects an "@<ref>" token, so strip the suffix from the project
    # and the pins for the run command (the label keeps the ref for clarity).
    run_project = _strip_git_ref(effective_project)
    run_pins = [_strip_git_ref(p) for p in pins]

    def _opp_env_run(inner):
        return run_external(
            _opp_env_cmd() + ["run", "-w", ws, *run_pins, run_project, "-c", inner],
            label=f"opp_env:{effective_project}", env=env, cwd=run_cwd,
            stream=True, on_output=recorder.output if recorder else None)

    # For kind=build the build *is* the test (opp_build_project has no
    # --no-build flag and there is nothing to run afterwards), so the build
    # stage carries the verdict and the test stage is skipped.
    build_only = kind == "build"

    with _workspace_lock(ws):
        # ── project.build ─────────────────────────────────────────────
        if recorder is not None:
            recorder.begin(Stage.PROJECT_BUILD, command=build_inner)
        start = time.time()
        build = _opp_env_run(build_inner)
        build_seconds = time.time() - start
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
        if build_only:
            if recorder is not None:
                recorder.skip(Stage.TEST_RUN, reason="kind=build: the build is the test")
            return {
                "result_code": "PASS",
                "test_exec_seconds": build_seconds,
                "stdout": build.stdout,
                "stderr": build.stderr,
                "details": None,
                "commit_sha": None,
            }
        # ── test.run ──────────────────────────────────────────────────
        # opp_repl writes result.to_dict() to a temp file via --result-file; we
        # read it into `details` (see plan §2.7). The flag is plumbing, so the
        # recorder display keeps the clean `test_inner`.
        if recorder is not None:
            recorder.begin(Stage.TEST_RUN, command=test_inner)
        fd, result_path = tempfile.mkstemp(prefix="opp-ci-result-", suffix=".json")
        os.close(fd)
        start = time.time()
        try:
            result = _opp_env_run(
                test_inner + f" --result-file {shlex.quote(result_path)}")
            duration = time.time() - start
            if recorder is not None:
                recorder.end(result.returncode)
            details = _read_result_file(result_path)
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    return {
        "result_code": result_code,
        "test_exec_seconds": duration,
        "stdout": (build.stdout or "") + (result.stdout or ""),
        "stderr": (build.stderr or "") + (result.stderr or ""),
        "details": details,
        "commit_sha": None,
    }


def _run_test_direct(project, kind, *, opp_file=None, git_ref=None, mode=None,
                     recorder=None, skip_build=False, **_unused):
    """Run a test by calling opp_repl functions directly (no subprocess).

    When *git_ref* is set, an isolated git worktree is created for that
    commit and removed after the test completes.

    Captured as stages (mirroring the opp_env path): an optional ``checkout``
    (worktree), then ``project.build`` (``simulation_project.build()``), then
    ``test.run`` (the test runner with ``build=False`` — no rebuild). A build
    failure fails the build stage and skips the test. For ``kind=build`` the
    build is the whole job, so there is no test stage.

    Extra kwargs (e.g. os, compiler) are accepted-and-ignored so this can sit
    downstream of the run_test dispatcher.
    """
    build_only = kind == "build"
    test_functions = _get_test_functions()
    func = None if build_only else test_functions.get(kind)
    if not build_only and func is None:
        raise ValueError(f"Unknown test kind: {kind!r}. Supported: {list(test_functions.keys())}")

    _ws, simulation_project = _load_workspace(project, opp_file)

    out_cb = (lambda text: recorder.output("out", text)) if recorder else None
    err_cb = (lambda text: recorder.output("err", text)) if recorder else None
    stdout_buf = _CallbackStringIO(out_cb) if out_cb else io.StringIO()
    stderr_buf = _CallbackStringIO(err_cb) if err_cb else io.StringIO()

    # ── checkout (worktree) ───────────────────────────────────────────
    worktree_path = None
    if git_ref:
        if recorder is not None:
            recorder.begin(Stage.CHECKOUT, command=f"git worktree @ {git_ref}")
        try:
            from opp_repl.simulation.project import make_worktree_simulation_project
            root = simulation_project.get_root_path()
            if root:
                run_external(["git", "fetch", "origin"], label="git fetch",
                             cwd=root, timeout=120, stream=True, on_output=out_cb)
            simulation_project = make_worktree_simulation_project(simulation_project, git_ref)
            worktree_path = simulation_project.get_root_path()
            _logger.info("Created worktree at %s for %s@%s", worktree_path, project, git_ref)
        except Exception as e:
            if recorder is not None:
                recorder.output("err", repr(e))
                recorder.end(1, status="failed")
            raise
        if recorder is not None:
            recorder.end(0)

    from opp_repl.common.util import ensure_logging_initialized
    ensure_logging_initialized("DEBUG", "DEBUG", None)
    _logger.info("Running %s test for %s (direct mode)", kind, project)

    build_mode = mode or "debug"
    start = time.time()
    try:
        # ── project.build ─────────────────────────────────────────────
        # Skipped when skip_build is set (the podman host path runs the build
        # as a separate exec; the test exec then runs build-less here).
        if not skip_build:
            if recorder is not None:
                recorder.begin(Stage.PROJECT_BUILD, command="opp_build_project (direct)")
            try:
                with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                    build_result = simulation_project.build(mode=build_mode)
            except Exception as e:
                _logger.error("Build for %s raised: %s", project, e)
                if recorder is not None:
                    recorder.output("err", repr(e))
                    recorder.end(1, status="failed")
                    if not build_only:
                        recorder.skip(Stage.TEST_RUN, reason="skipped: build failed")
                return {
                    "result_code": "ERROR", "test_exec_seconds": time.time() - start,
                    "stdout": stdout_buf.getvalue(),
                    "stderr": stderr_buf.getvalue() + "\n" + repr(e),
                    "details": None, "commit_sha": git_ref,
                }
            build_failed = (build_result is not None
                            and hasattr(build_result, "is_all_results_expected")
                            and not build_result.is_all_results_expected())
            if recorder is not None:
                recorder.end(1 if build_failed else 0,
                             status="failed" if build_failed else None)
            if build_failed:
                if recorder is not None and not build_only:
                    recorder.skip(Stage.TEST_RUN, reason="skipped: build failed")
                return {
                    "result_code": "FAIL", "test_exec_seconds": time.time() - start,
                    "stdout": stdout_buf.getvalue(), "stderr": stderr_buf.getvalue(),
                    "details": None, "commit_sha": git_ref or resolve_commit_sha(project, opp_file=opp_file),
                }
            if build_only:
                # The build is the whole job — no test stage.
                commit_sha = git_ref or resolve_commit_sha(project, opp_file=opp_file)
                return {
                    "result_code": "PASS", "test_exec_seconds": time.time() - start,
                    "stdout": stdout_buf.getvalue(), "stderr": stderr_buf.getvalue(),
                    "details": None, "commit_sha": commit_sha,
                }

        # kind=build has no test stage — the build is the whole job. When
        # skip_build is set (podman host path: the build ran as a separate
        # exec), we reach here without the build block's early return above, so
        # finish PASS rather than call the (None) test function.
        if build_only:
            commit_sha = git_ref or resolve_commit_sha(project, opp_file=opp_file)
            return {
                "result_code": "PASS", "test_exec_seconds": time.time() - start,
                "stdout": stdout_buf.getvalue(), "stderr": stderr_buf.getvalue(),
                "details": None, "commit_sha": commit_sha,
            }

        # ── test.run (build already done; don't rebuild) ──────────────
        if recorder is not None:
            recorder.begin(Stage.TEST_RUN, command=f"{func.__name__} --no-build (direct)")
        call_kwargs = {"simulation_project": simulation_project, "build": False}
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
        if recorder is not None:
            recorder.end(0 if result_code == "PASS" else 1)

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
