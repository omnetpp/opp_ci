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

try:
    from opp_ci._version import __version__ as _WORKER_VERSION
except ImportError:
    _WORKER_VERSION = "0.0.0"

_logger = logging.getLogger(__name__)


class _RunOutputStreamer:
    """Ships a running test's stage events to the coordinator for live view.

    The :py:class:`~opp_ci.stages.StageRecorder` calls :py:meth:`append` with
    each stage event (stage_begin / output / stage_end). A background thread
    flushes the accumulated events to ``/api/runs/{id}/output-append`` every
    flush interval, so the run-detail page sees stages and output within a
    couple of seconds rather than only at completion. Best-effort: a failed
    POST drops that batch — the full, authoritative stdout/stderr is still
    reported when the run finishes, so the live view is purely a convenience.

    Uses its own session; the flush thread is the only one that posts (the
    final flush in :py:meth:`stop` runs after the thread is joined), so the
    session is never used concurrently.
    """

    def __init__(self, session, coordinator_url, run_id):
        self._session = session
        self._url = f"{coordinator_url.rstrip('/')}/api/runs/{run_id}/output-append"
        self._run_id = run_id
        self._pending = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def append(self, event):
        """Queue one stage event (dict). Called from the executor, including
        its stream-pump threads, so it must stay cheap and thread-safe."""
        with self._lock:
            self._pending.append(event)

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"opp_ci-runout-{self._run_id}", daemon=True)
        self._thread.start()

    def _drain(self):
        with self._lock:
            batch, self._pending = self._pending, []
        return batch

    def _flush(self):
        batch = self._drain()
        if not batch:
            return
        try:
            self._session.post(self._url, json={"events": batch}, timeout=10)
        except requests.RequestException as e:
            # Dropped on purpose — _report_result still sends the full output.
            _logger.debug("Run #%d output ship failed: %s", self._run_id, e)

    def _loop(self):
        from opp_ci import config as cfg
        while not self._stop.wait(cfg.RUN_OUTPUT_FLUSH_INTERVAL):
            self._flush()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._flush()  # ship whatever's left


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
        # Log shipping: a ring handler captures this worker's recent log
        # records (installed in start()); the heartbeat thread ships the new
        # ones to the coordinator. _last_shipped_seq is touched only by that
        # thread, so it needs no extra locking.
        self._log_handler = None
        self._last_shipped_seq = 0

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

        self._install_log_handler()
        self._reap_leaked_containers()

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

        # Stop the heartbeat thread and wait briefly for it to exit, then send
        # a final goodbye so the coordinator marks us offline immediately
        # (joining first ensures no normal heartbeat re-bumps us online after).
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        self._send_offline()

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

    def _reap_leaked_containers(self):
        """Remove opp_ci runner containers left behind by a previous crash.

        Safe to run at startup: no job is executing yet, so any container named
        ``opp_ci_run_*`` is orphaned (its `finally` teardown never ran because
        the worker was killed mid-run). Best-effort — a quiet no-op when podman
        isn't installed or there's nothing to reap.
        """
        import subprocess
        try:
            ps = subprocess.run(
                ["podman", "ps", "-aq", "--filter", "name=^opp_ci_run_"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            _logger.debug("Container reap skipped: %s", e)
            return
        ids = ps.stdout.split()
        if not ids:
            return
        _logger.info("Reaping %d leaked runner container(s) from a prior crash", len(ids))
        try:
            subprocess.run(["podman", "rm", "-f", *ids],
                           capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            _logger.warning("Failed to reap leaked containers: %s", e)

    def _install_log_handler(self):
        """Attach the ring-buffer handler to the `opp_ci` logger.

        Scoped to `opp_ci` (not root) so it captures this worker's own
        records — including the executor's streamed build/compile/test
        output, which is emitted via `_logger.info` — but not third-party
        chatter. Best-effort: a failure here must not stop the worker.
        """
        try:
            from opp_ci import config as cfg
            from opp_ci.logbuffer import RingBufferHandler
            handler = RingBufferHandler(cfg.WORKER_LOG_RING)
            logging.getLogger("opp_ci").addHandler(handler)
            self._log_handler = handler
        except Exception as e:  # noqa: BLE001 — never block startup on this
            _logger.warning("Could not install log-shipping handler: %s", e)

    def _collect_log_batch(self):
        """New log records since the last shipped batch, capped, with a drop
        marker. Returns {"entries": [...], "high_seq": int} or None when
        there's nothing new (or no handler). Does not advance the shipped
        watermark — the caller does that only after a successful POST."""
        if self._log_handler is None:
            return None
        from opp_ci import config as cfg
        entries, dropped = self._log_handler.since(
            self._last_shipped_seq, limit=cfg.WORKER_LOG_BATCH)
        if not entries:
            return None
        out = [{"ts": e["ts"], "level": e["level"], "msg": e["msg"]}
               for e in entries]
        if dropped:
            out.insert(0, {
                "ts": entries[0]["ts"],
                "level": logging.WARNING,
                "msg": f"… {dropped} earlier log line(s) dropped (batch cap) …",
            })
        return {"entries": out, "high_seq": entries[-1]["seq"]}

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
        batch = self._collect_log_batch()
        body = {"version": _WORKER_VERSION}
        if batch:
            body["logs"] = {"entries": batch["entries"]}
        try:
            resp = session.post(
                f"{self.coordinator_url}/api/workers/heartbeat",
                json=body,
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning("Heartbeat failed: %s %s", resp.status_code, resp.text)
                return
            # A busy worker isn't polling, so the heartbeat is its path to learn
            # of an admin-requested shutdown. Parse defensively — a malformed
            # body must never break heartbeating.
            try:
                if resp.json().get("command") == "shutdown":
                    self._request_stop("coordinator requested shutdown")
            except ValueError:
                pass
        except requests.RequestException as e:
            _logger.warning("Heartbeat error: %s", e)
            return
        # Advance the watermark only after the coordinator has the batch, so
        # a failed heartbeat re-ships the same lines next beat.
        if batch:
            self._last_shipped_seq = batch["high_seq"]

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
            if data.get("command") == "shutdown":
                self._request_stop("coordinator requested shutdown")
                return None
            return data.get("job")
        except requests.RequestException as e:
            _logger.warning("Poll error: %s", e)
            return None

    def _execute(self, job):
        """Execute a job and report the result back to the coordinator."""
        from opp_ci.persistence import capture_system_snapshot

        run_id = job["run_id"]
        # Build the full opp_env project id from (project, version) — `version`
        # is opp_env's version *field* (e.g. "git"), so "mm1k" + "git" -> the
        # id "mm1k-git", not "git". (Fixes opp_env install … git-latest.)
        from opp_ci.executor import opp_env_project_id
        project = opp_env_project_id(job["project"], job.get("version"))
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

        # Capture the run as ordered stages (deps.install, project.build,
        # test.run, …) and stream their events live to the coordinator so the
        # run-detail page can watch progress (best-effort; the full output is
        # still reported at completion). The streamer has its own session and
        # is stopped on every exit path via the finally below.
        from opp_ci.stages import StageRecorder
        streamer = _RunOutputStreamer(self._make_session(), self.coordinator_url, run_id)
        streamer.start()
        recorder = StageRecorder(on_event=streamer.append)
        try:
            try:
                install_project(project, git_ref=git_ref,
                                isolation=isolation, toolchain=toolchain,
                                resolved_deps=run_kwargs["resolved_deps"],
                                compiler=run_kwargs["compiler"],
                                compiler_version=run_kwargs["compiler_version"],
                                recorder=recorder)
            except RuntimeError as e:
                _logger.error("Install failed for run #%d: %s", run_id, e)
                self._report_result(run_id, "ERROR", stderr=str(e),
                                    stages=recorder.stages)
                return

            try:
                outcome = run_test(project, kind, recorder=recorder, **run_kwargs)
            except Exception as e:
                _logger.error("Test execution failed for run #%d: %s", run_id, e)
                self._report_result(run_id, "ERROR", stderr=str(e),
                                    stages=recorder.stages)
                return

            self._report_result(
                run_id,
                outcome["result_code"],
                test_exec_seconds=outcome["test_exec_seconds"],
                commit_sha=outcome.get("commit_sha"),
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
                details=outcome.get("details"),
                stages=recorder.stages,
            )
            _logger.info("Run #%d completed: %s (%.1fs)", run_id, outcome["result_code"], outcome["test_exec_seconds"])
        finally:
            streamer.stop()

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
                       commit_sha=None, stdout=None, stderr=None, details=None,
                       stages=None):
        """Report a job result back to the coordinator.

        ``stages`` is the recorder's assembled stage tree; the coordinator
        persists it as TestRunStage rows for the finished staged view.
        """
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
        if stages:
            payload["stages"] = stages

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

    def _send_offline(self):
        """Best-effort final heartbeat on shutdown, flagged so the coordinator
        marks us offline at once instead of waiting for the heartbeat-timeout
        reaper to notice. Never raises — shutdown must not hang on the network.
        """
        try:
            self._session.post(
                f"{self.coordinator_url}/api/workers/heartbeat",
                json={"going_offline": True, "version": _WORKER_VERSION},
                timeout=10,
            )
        except requests.RequestException as e:
            _logger.warning("Final offline heartbeat failed: %s", e)

    def _request_stop(self, reason):
        """Stop the main loop and heartbeat thread gracefully.

        A job already executing runs to completion (the main loop just won't
        poll again once `_running` is False); the heartbeat thread's
        `_stop_event.wait` returns and it exits too. Shared by the signal
        handler and the coordinator-requested shutdown path.
        """
        _logger.info("Shutting down: %s", reason)
        self._running = False
        self._stop_event.set()

    def _handle_signal(self, signum, frame):
        self._request_stop(f"received signal {signum}")
