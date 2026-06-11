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
import os
import signal
import threading
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
        self._session = self._make_session()
        # Set when shutting down; also used by the heartbeat thread to sleep
        # interruptibly so a stop request is honored promptly.
        self._stop_event = threading.Event()
        self._heartbeat_thread = None

    def _make_session(self):
        """Build an authenticated, configured requests session."""
        from opp_ci.http import configure_session
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self.token}"
        configure_session(session)
        return session

    def fetch_config(self):
        """Fetch this worker's registered name/tags/concurrency from the coordinator."""
        resp = self._session.get(
            f"{self.coordinator_url}/api/workers/me",
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

    def start(self, poll_interval=10, heartbeat_interval=30, niceness=10):
        """
        Main loop: poll for jobs and execute them.

        Heartbeats run on a dedicated daemon thread so they keep flowing even
        while a job blocks this thread for minutes (e.g. a long compile);
        otherwise the coordinator would mark a busy worker offline and reclaim
        the run it is still executing.

        ``niceness`` lowers the worker's scheduling priority; the build/test
        subprocesses it spawns inherit it, so CI work yields to interactive use
        on a shared host. Pass 0 to keep normal priority.
        """
        if niceness:
            self._apply_niceness(niceness)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        _logger.info(
            "Worker '%s' starting — coordinator=%s tags=%s concurrency=%d",
            self.name, self.coordinator_url, self.tags, self.concurrency,
        )

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_interval,),
            name="opp_ci-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        while self._running:
            # Poll for a job
            job = self._poll()
            if job:
                self._execute(job)
            else:
                time.sleep(poll_interval)

        # Stop the heartbeat thread and wait briefly for it to exit.
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)

        _logger.info("Worker stopped.")

    def _apply_niceness(self, niceness):
        """Raise the nice level (lower the priority) of this process.

        Best-effort: an unprivileged process can always increase its own
        niceness, so this normally succeeds, but we never let a scheduling
        tweak stop the worker from running.
        """
        try:
            new_nice = os.nice(niceness)
            _logger.info("Worker running at nice level %d", new_nice)
        except (OSError, AttributeError) as e:
            _logger.warning("Could not set niceness to %d: %s", niceness, e)

    def _heartbeat_loop(self, heartbeat_interval):
        """Send a heartbeat immediately, then every heartbeat_interval seconds
        until shutdown. Uses its own session to avoid contending with the main
        loop's poll/report requests."""
        session = self._make_session()
        # Beat once up front so a freshly-started worker comes online without
        # waiting a full interval.
        self._heartbeat(session)
        while not self._stop_event.wait(heartbeat_interval):
            self._heartbeat(session)

    def _heartbeat(self, session=None):
        session = session or self._session
        try:
            resp = session.post(
                f"{self.coordinator_url}/api/workers/heartbeat",
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning("Heartbeat failed: %s %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            _logger.warning("Heartbeat error: %s", e)

    def _poll(self):
        """Poll the coordinator for a job. Returns the job dict or None."""
        try:
            resp = self._session.post(
                f"{self.coordinator_url}/api/workers/poll",
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
        from opp_ci.persistence import capture_system_snapshot

        run_id = job["run_id"]
        # If the run has a specific version (e.g. "inet-4.5"), use it as the
        # opp_env project identifier; otherwise fall back to the bare project
        # name (e.g. "mm1k") for projects with a single version.
        project = job.get("version") or job["project"]
        kind = job["kind"]
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
            run_id, project, kind, git_ref, isolation, toolchain,
        )

        # Best-effort: capture system snapshot before running.
        try:
            snapshot = capture_system_snapshot()
            self._report_snapshot(run_id, snapshot)
        except Exception as e:
            _logger.warning("Snapshot capture failed for run #%d: %s", run_id, e)

        try:
            install_project(project, git_ref=git_ref,
                            isolation=isolation, toolchain=toolchain,
                            resolved_deps=run_kwargs["resolved_deps"],
                            compiler=run_kwargs["compiler"],
                            compiler_version=run_kwargs["compiler_version"])
        except RuntimeError as e:
            _logger.error("Install failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        try:
            outcome = run_test(project, kind, **run_kwargs)
        except Exception as e:
            _logger.error("Test execution failed for run #%d: %s", run_id, e)
            self._report_result(run_id, "ERROR", stderr=str(e))
            return

        self._report_result(
            run_id,
            outcome["result_code"],
            test_exec_seconds=outcome["test_exec_seconds"],
            commit_sha=outcome.get("commit_sha"),
            stdout=outcome["stdout"],
            stderr=outcome["stderr"],
            details=outcome.get("details"),
        )
        _logger.info("Run #%d completed: %s (%.1fs)", run_id, outcome["result_code"], outcome["test_exec_seconds"])

    def _report_snapshot(self, run_id, snapshot):
        try:
            resp = self._session.post(
                f"{self.coordinator_url}/api/workers/snapshot",
                json={"run_id": run_id, "snapshot": snapshot},
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning("Snapshot report failed for run #%d: %s %s",
                                run_id, resp.status_code, resp.text)
        except requests.RequestException as e:
            _logger.warning("Snapshot report error for run #%d: %s", run_id, e)

    def _report_result(self, run_id, result_code, test_exec_seconds=None,
                       commit_sha=None, stdout=None, stderr=None, details=None):
        """Report a job result back to the coordinator."""
        payload = {
            "run_id": run_id,
            "result_code": result_code,
        }
        if test_exec_seconds is not None:
            payload["test_exec_seconds"] = test_exec_seconds
        if commit_sha:
            payload["commit_sha"] = commit_sha
        if stdout:
            payload["stdout"] = stdout
        if stderr:
            payload["stderr"] = stderr
        if details:
            payload["details"] = details

        try:
            resp = self._session.post(
                f"{self.coordinator_url}/api/workers/result",
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
        self._stop_event.set()
