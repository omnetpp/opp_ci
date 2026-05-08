import json
import logging
import os
import subprocess
import tempfile
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
    "all": "opp_run_all_tests",
}


def resolve_git_project(project, git_ref):
    """
    Resolve the opp_env project name and git ref for testing.

    If git_ref is specified:
      - For opp_env mode: uses '<project>-git' package and sets the ref via env var
      - For direct mode: checks out the ref in the project working copy

    Returns (effective_project, effective_ref) tuple.
    """
    if not git_ref:
        return project, None
    if USE_OPP_ENV:
        effective_project = f"{project}-git" if not project.endswith("-git") else project
        return effective_project, git_ref
    return project, git_ref


def checkout_ref(project, git_ref):
    """
    Check out a specific git ref in the project working copy (direct mode).

    Uses OPP_CI_PROJECT_DIR_<PROJECT> env var to find the working copy,
    falling back to OPP_CI_PROJECT_DIR/<project>.
    """
    env_key = f"OPP_CI_PROJECT_DIR_{project.upper().replace('-', '_')}"
    project_dir = os.environ.get(env_key)
    if not project_dir:
        base_dir = os.environ.get("OPP_CI_PROJECT_DIR", ".")
        project_dir = os.path.join(base_dir, project)

    _logger.info("Checking out ref %s in %s", git_ref, project_dir)
    result = subprocess.run(
        ["git", "checkout", git_ref],
        cwd=project_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        _logger.error("git checkout failed:\n%s", result.stderr)
        raise RuntimeError(f"git checkout {git_ref} in {project_dir} failed: {result.stderr.strip()}")
    _logger.info("Checked out %s", git_ref)
    return project_dir


def install_project(project, git_ref=None):
    """Install a project via opp_env. No-op in direct mode."""
    if not USE_OPP_ENV:
        if git_ref:
            checkout_ref(project, git_ref)
        else:
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


def run_test(project, test_type, git_ref=None, opp_file=None):
    """
    Run a test for the given project.

    In direct mode (USE_OPP_ENV=0): runs the opp_repl command directly.
    In opp_env mode (USE_OPP_ENV=1): runs via opp_env run <project> -c <cmd>.

    If git_ref is provided:
      - opp_env mode: uses <project>-git and sets OPP_ENV_GIT_REF env var
      - direct mode: assumes checkout_ref was already called during install

    Returns a dict with keys: result_code, duration_seconds, stdout, stderr, details.
    """
    cmd = COMMAND_MAP.get(test_type)
    if cmd is None:
        raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(COMMAND_MAP.keys())}")

    env = os.environ.copy()
    result_file = None
    if USE_OPP_ENV:
        effective_project, effective_ref = resolve_git_project(project, git_ref)
        if effective_ref:
            env["OPP_ENV_GIT_REF"] = effective_ref
        args = ["opp_env", "run", effective_project, "-c", cmd]
    else:
        result_file = tempfile.NamedTemporaryFile(
            prefix="opp_ci_result_", suffix=".json", delete=False
        )
        result_file.close()
        args = [cmd, "--load", "@opp"]
        if opp_file:
            args += ["--load", opp_file]
        args += ["-p", project, "--result-file", result_file.name]

    _logger.info("Running test: %s", " ".join(args))
    start = time.time()
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    duration = time.time() - start

    if result.returncode == 0:
        result_code = "PASS"
    else:
        result_code = "FAIL"

    details = None
    if result_file:
        try:
            with open(result_file.name, "r") as f:
                details = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("Failed to read result file %s: %s", result_file.name, e)
        finally:
            try:
                os.unlink(result_file.name)
            except OSError:
                pass

    _logger.info("Test finished: %s (%.1fs)", result_code, duration)
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "details": details,
    }
