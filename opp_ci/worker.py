"""
Worker agent for opp_ci Stage 5.

Polls the coordinator for queued jobs, executes them locally via
opp_env + opp_repl, and reports results back.

Usage:
    opp_ci worker start --coordinator <url> --token <token>

Tags and concurrency are configured at registration time (see
`opp_ci worker register`) and fetched from the coordinator on startup —
the coordinator is the single source of truth.
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

    def __init__(self, coordinator_url, token):
        self.coordinator_url = coordinator_url.rstrip("/")
        self.token = token
        self.name = None
        self.tags = []
        self.concurrency = 1
        self._running = True
        self._headers = {"Authorization": f"Bearer {token}"}

    def fetch_config(self):
        """Fetch this worker's registered name/tags/concurrency from the coordinator."""
        resp = requests.get(
            f"{self.coordinator_url}/api/workers/me",
            headers=self._headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch worker config from coordinator: "
                f"{resp.status_code} {resp.text}"
            )
        data = resp.json()
        self.name = data["name"]
        self.tags = data.get("tags") or []
        self.concurrency = data.get("concurrency", 1)

    def start(self, poll_interval=10, heartbeat_interval=30):
        """
        Main loop: send heartbeats and poll for jobs.
        """
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        _logger.info(
            "Worker '%s' starting — coordinator=%s tags=%s concurrency=%d",
            self.name, self.coordinator_url, self.tags, self.concurrency,
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
        # If the run has a specific version (e.g. "inet-4.5"), use it as the
        # opp_env project identifier; otherwise fall back to the bare project
        # name (e.g. "mm1k") for projects with a single version.
        project = job.get("version") or job["project"]
        test = job["test"]
        git_ref = job.get("git_ref")
        opp_file = job.get("opp_file")
        mode = job.get("mode")
        isolation = job.get("isolation") or "none"
        toolchain = job.get("toolchain") or "none"
        run_kwargs = {
            "git_ref": git_ref,
            "opp_file": opp_file,
            "mode": mode,
            "isolation": isolation,
            "toolchain": toolchain,
            "os": job.get("os"),
            "os_version": job.get("os_version"),
            "distro": job.get("distro"),
            "distro_version": job.get("distro_version"),
            "flavor": job.get("flavor"),
            "flavor_version": job.get("flavor_version"),
            "arch": job.get("arch"),
            "compiler": job.get("compiler"),
            "compiler_version": job.get("compiler_version"),
            "resolved_deps": job.get("resolved_deps"),
        }

        _logger.info(
            "Executing run #%d: %s / %s (ref=%s, isolation=%s, toolchain=%s)",
            run_id, project, test, git_ref, isolation, toolchain,
        )

        try:
            install_project(project, git_ref=git_ref,
                            isolation=isolation, toolchain=toolchain)
        except RuntimeError as e:
            _logger.error("Install failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        try:
            outcome = run_test(project, test, **run_kwargs)
        except Exception as e:
            _logger.error("Test execution failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        self._report_result(
            run_id,
            outcome["result_code"],
            duration_seconds=outcome["duration_seconds"],
            commit_sha=outcome.get("commit_sha"),
            stdout=outcome["stdout"],
            stderr=outcome["stderr"],
            details=outcome.get("details"),
        )
        _logger.info("Run #%d completed: %s (%.1fs)", run_id, outcome["result_code"], outcome["duration_seconds"])

    def _report_result(self, run_id, result_code, duration_seconds=None,
                       commit_sha=None, stdout=None, stderr=None, details=None):
        """Report a job result back to the coordinator."""
        payload = {
            "run_id": run_id,
            "result_code": result_code,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if commit_sha:
            payload["commit_sha"] = commit_sha
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
