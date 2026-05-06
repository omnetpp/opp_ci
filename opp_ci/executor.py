import logging
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
    "all": "opp_run_all_tests",
}


def install_project(project):
    """Install a project via opp_env. No-op in direct mode."""
    if not USE_OPP_ENV:
        _logger.info("Skipping install (direct mode, OPP_CI_USE_OPP_ENV=0)")
        return
    _logger.info("Installing %s via opp_env", project)
    result = subprocess.run(
        ["opp_env", "install", project],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        _logger.error("opp_env install failed:\n%s", result.stderr)
        raise RuntimeError(f"opp_env install {project} failed (exit code {result.returncode})")
    _logger.info("Installation of %s complete", project)


def run_test(project, test_type):
    """
    Run a test for the given project.

    In direct mode (USE_OPP_ENV=0): runs the opp_repl command directly.
    In opp_env mode (USE_OPP_ENV=1): runs via opp_env run <project> -c <cmd>.

    Returns a dict with keys: result_code, duration_seconds, stdout, stderr.
    """
    cmd = COMMAND_MAP.get(test_type)
    if cmd is None:
        raise ValueError(f"Unknown test type: {test_type!r}. Supported: {list(COMMAND_MAP.keys())}")

    if USE_OPP_ENV:
        args = ["opp_env", "run", project, "-c", cmd]
    else:
        args = [cmd, "--load", "@opp", "-p", project]

    _logger.info("Running test: %s", " ".join(args))
    start = time.time()
    result = subprocess.run(args, capture_output=True, text=True)
    duration = time.time() - start

    if result.returncode == 0:
        result_code = "PASS"
    else:
        result_code = "FAIL"

    _logger.info("Test finished: %s (%.1fs)", result_code, duration)
    return {
        "result_code": result_code,
        "duration_seconds": duration,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
