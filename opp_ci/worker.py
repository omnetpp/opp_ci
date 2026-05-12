"""
Worker agent for opp_ci Stage 5.

Polls the coordinator for queued jobs, executes them locally via
opp_env + opp_repl, and reports results back.

Usage:
    opp_ci worker start --coordinator <url> --token <token> --tags linux,amd64
"""

import logging
import signal
import time

import requests

from opp_ci.executor import install_project, run_test

_logger = logging.getLogger(__name__)


class WorkerAgent:
    """
    Long-running worker that polls the coordinator for jobs and executes them.
    """

    def __init__(self, coordinator_url, token, tags=None, concurrency=1):
        self.coordinator_url = coordinator_url.rstrip("/")
        self.token = token
        self.tags = tags or []
        self.concurrency = concurrency
        self._running = True
        self._headers = {"Authorization": f"Bearer {token}"}

    def start(self, poll_interval=10, heartbeat_interval=30):
        """
        Main loop: send heartbeats and poll for jobs.
        """
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        _logger.info(
            "Worker starting — coordinator=%s tags=%s concurrency=%d",
            self.coordinator_url, self.tags, self.concurrency,
        )

        last_heartbeat = 0
        while self._running:
            now = time.time()

            # Heartbeat
            if now - last_heartbeat >= heartbeat_interval:
                self._heartbeat()
                last_heartbeat = now

            # Poll for a job
            job = self._poll()
            if job:
                self._execute(job)
            else:
                time.sleep(poll_interval)

        _logger.info("Worker stopped.")

    def _heartbeat(self):
        try:
            resp = requests.post(
                f"{self.coordinator_url}/api/workers/heartbeat",
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning("Heartbeat failed: %s %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            _logger.warning("Heartbeat error: %s", e)

    def _poll(self):
        """Poll the coordinator for a job. Returns the job dict or None."""
        try:
            resp = requests.post(
                f"{self.coordinator_url}/api/workers/poll",
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning("Poll failed: %s %s", resp.status_code, resp.text)
                return None
            data = resp.json()
            return data.get("job")
        except requests.RequestException as e:
            _logger.warning("Poll error: %s", e)
            return None

    def _execute(self, job):
        """Execute a job and report the result back to the coordinator."""
        run_id = job["run_id"]
        project = job["project"]
        test_type = job["test_type"]
        git_ref = job.get("git_ref")
        opp_file = job.get("opp_file")

        _logger.info("Executing run #%d: %s / %s (ref=%s)", run_id, project, test_type, git_ref)

        try:
            install_project(project, git_ref=git_ref)
        except RuntimeError as e:
            _logger.error("Install failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        try:
            outcome = run_test(project, test_type, git_ref=git_ref, opp_file=opp_file)
        except Exception as e:
            _logger.error("Test execution failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        self._report_result(
            run_id,
            outcome["result_code"],
            duration_seconds=outcome["duration_seconds"],
            stdout=outcome["stdout"],
            stderr=outcome["stderr"],
            details=outcome.get("details"),
        )
        _logger.info("Run #%d completed: %s (%.1fs)", run_id, outcome["result_code"], outcome["duration_seconds"])

    def _report_result(self, run_id, result_code, duration_seconds=None,
                       stdout=None, stderr=None, details=None):
        """Report a job result back to the coordinator."""
        payload = {
            "run_id": run_id,
            "result_code": result_code,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if stdout:
            payload["stdout"] = stdout
        if stderr:
            payload["stderr"] = stderr
        if details:
            payload["details"] = details

        try:
            resp = requests.post(
                f"{self.coordinator_url}/api/workers/result",
                headers=self._headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code != 200:
                _logger.error("Result report failed for run #%d: %s %s", run_id, resp.status_code, resp.text)
        except requests.RequestException as e:
            _logger.error("Result report error for run #%d: %s", run_id, e)

    def _handle_signal(self, signum, frame):
        _logger.info("Received signal %d, shutting down...", signum)
        self._running = False
