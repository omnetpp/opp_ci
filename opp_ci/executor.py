import contextlib
import io
import logging
import os
import subprocess
import time

from opp_ci.config import USE_OPP_ENV

_logger = logging.getLogger(__name__)

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
    """Create a fresh SimulationWorkspace, load .opp files, and return (workspace, project)."""
    from opp_repl.simulation.workspace import SimulationWorkspace
    ws = SimulationWorkspace()
    ws.load_opp_file("@opp")
    if opp_file:
        ws.load_opp_file(opp_file)
    simulation_project = ws.determine_default_simulation_project(name=project_name)
    return ws, simulation_project


def resolve_git_project(project, git_ref):
    """
    Resolve the opp_env project name and git ref for testing.

    If git_ref is specified and opp_env mode is active, uses '<project>-git'
    package and sets the ref via env var.

    Returns (effective_project, effective_ref) tuple.
    """
    if not git_ref:
        return project, None
    if USE_OPP_ENV:
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


def install_project(project, git_ref=None):
    """Install a project via opp_env. No-op in direct mode."""
    if not USE_OPP_ENV:
        _logger.info("Skipping install (direct mode, OPP_CI_USE_OPP_ENV=0)")
        return

    effective_project, _ = resolve_git_project(project, git_ref)
    _logger.info("Installing %s via opp_env", effective_project)
    result = subprocess.run(
        ["opp_env", "install", effective_project],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        _logger.error("opp_env install failed:\n%s", result.stderr)
        raise RuntimeError(f"opp_env install {effective_project} failed (exit code {result.returncode})")
    _logger.info("Installation of %s complete", effective_project)


def run_test(project, test_type, git_ref=None, opp_file=None, mode=None):
    """
    Run a test for the given project.

    In direct mode (USE_OPP_ENV=0): calls opp_repl functions directly in-process.
    In opp_env mode (USE_OPP_ENV=1): runs via opp_env run <project> -c <cmd>.

    If git_ref is provided:
      - opp_env mode: uses <project>-git and sets OPP_ENV_GIT_REF env var
      - direct mode: creates an isolated git worktree for that ref

    Returns a dict with keys: result_code, duration_seconds, stdout, stderr, details.
    """
    if USE_OPP_ENV:
        return _run_test_via_opp_env(project, test_type, git_ref, mode=mode)
    else:
        return _run_test_direct(project, test_type, opp_file, git_ref, mode=mode)


def _run_test_via_opp_env(project, test_type, git_ref, mode=None):
    """Run a test via opp_env subprocess (isolated Nix environment)."""
    cmd = COMMAND_MAP.get(test_type)
    if cmd is None:
        raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(COMMAND_MAP.keys())}")

    if mode:
        cmd += f" --mode {mode}"

    env = os.environ.copy()
    effective_project, effective_ref = resolve_git_project(project, git_ref)
    if effective_ref:
        env["OPP_ENV_GIT_REF"] = effective_ref
    args = ["opp_env", "run", effective_project, "-c", cmd]

    _logger.info("Running test: %s", " ".join(args))
    start = time.time()
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    duration = time.time() - start

    result_code = "PASS" if result.returncode == 0 else "FAIL"
    _logger.info("Test finished: %s (%.1fs)", result_code, duration)
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "details": None,
        "commit_sha": None,
    }


def _run_test_direct(project, test_type, opp_file, git_ref=None, mode=None):
    """Run a test by calling opp_repl functions directly (no subprocess).

    When *git_ref* is set, an isolated git worktree is created for that
    commit and removed after the test completes.
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

    _logger.info("Running %s test for %s (direct mode)", test_type, project)
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    start = time.time()
    try:
        kwargs = {"simulation_project": simulation_project, "build": "task", "build_mode": "task"}
        if mode:
            kwargs["mode"] = mode
        if test_type == "opp":
            kwargs["test_folder"] = simulation_project.get_full_path(".")
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            result = func(**kwargs)
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
