"""Tests for orphaned `running` run recovery and poison-pill retirement.

Covers persistence.{reclaim_orphaned_runs, mark_stale_workers_offline,
retire_poison_run} and the relaxed finalize_verdict_for_run lifecycle guard:
  * a stale online/busy worker is flipped offline and its running runs are
    re-queued (reclaim_count bumped, worker_id/started_at cleared)
  * a freshly-registered worker (offline, no heartbeat) is left alone
  * a fresh, live worker (recent heartbeat) is left alone
  * a run reclaimed past MAX_RECLAIMS is retired to timed_out/ERROR and its
    parent TestMatrixRun completes with a verdict instead of hanging open
  * finalize_verdict_for_run promotes a timed_out run's verdict cell

Run with: python -m unittest tests.test_orphan_recovery   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_orphan_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["OPP_CI_REMOTE"] = "0"

from opp_ci.db.connection import engine, SessionLocal            # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    Base, TestResultCode, TestRunLifecycle, Worker,
)
from opp_ci.persistence import (                                 # noqa: E402
    create_matrix_from_axes, create_matrix_run, create_test_run,
    create_test_verdict, finalize_verdict_for_run, get_or_create_test,
    mark_stale_workers_offline, reclaim_orphaned_runs,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)
_OLD = _NOW - datetime.timedelta(seconds=9999)


def _coord(**over):
    base = {"project": "mm1k", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


class OrphanRecoveryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _worker(self, *, status, heartbeat, name="w1", concurrency=1, jobs=0):
        w = Worker(name=name, token="tok-" + name, concurrency=concurrency,
                   status=status, current_job_count=jobs, last_heartbeat=heartbeat)
        self.s.add(w)
        self.s.flush()
        return w

    def _running_run(self, worker, *, in_matrix=True):
        test = get_or_create_test(self.s, _coord())
        matrix_run_id = None
        if in_matrix:
            mtx = create_matrix_from_axes(self.s, project="mm1k", config={})
            mr = create_matrix_run(self.s, matrix_id=mtx.id)
            matrix_run_id = mr.id
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=matrix_run_id)
        if in_matrix:
            create_test_verdict(self.s, matrix_run_id=matrix_run_id,
                                test_id=test.id, test_run_id=run.id)
        run.lifecycle = TestRunLifecycle.running
        run.worker_id = worker.id
        run.started_at = _NOW
        self.s.flush()
        return run, matrix_run_id

    # ── reaper selection ───────────────────────────────────────────────

    def test_stale_worker_requeues_running_run(self):
        w = self._worker(status="busy", heartbeat=_OLD, jobs=1)
        run, _ = self._running_run(w, in_matrix=False)

        res = mark_stale_workers_offline(self.s, _NOW, 120, 2)
        self.s.commit()   # functions are transaction-neutral; caller commits

        self.assertEqual(res, [("w1", 1, 0)])
        self.s.refresh(run)
        self.s.refresh(w)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)
        self.assertIsNone(run.worker_id)
        self.assertIsNone(run.started_at)
        self.assertEqual(run.reclaim_count, 1)
        self.assertEqual(w.status, "offline")
        self.assertEqual(w.current_job_count, 0)

    def test_fresh_registered_worker_left_alone(self):
        # status offline + no heartbeat is the post-register state; the
        # online/busy filter must not match it (nothing to reclaim).
        self._worker(status="offline", heartbeat=None)
        res = mark_stale_workers_offline(self.s, _NOW, 120, 2)
        self.assertEqual(res, [])

    def test_live_worker_left_alone(self):
        w = self._worker(status="busy", heartbeat=_NOW, jobs=1)
        run, _ = self._running_run(w, in_matrix=False)
        res = mark_stale_workers_offline(self.s, _NOW, 120, 2)
        self.assertEqual(res, [])
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.running)

    # ── poison pill ────────────────────────────────────────────────────

    def test_poison_pill_retired_and_matrix_completes(self):
        w = self._worker(status="busy", heartbeat=_OLD, jobs=1)
        run, mr_id = self._running_run(w, in_matrix=True)

        # Burn through the reclaim budget (MAX_RECLAIMS=2): two re-queues...
        for expected in (1, 2):
            run.lifecycle = TestRunLifecycle.running
            run.worker_id = w.id
            requeued, retired = reclaim_orphaned_runs(self.s, w.id, _NOW, 2)
            self.s.commit()
            self.assertEqual((requeued, retired), (1, 0))
            self.s.refresh(run)
            self.assertEqual(run.reclaim_count, expected)
            self.assertEqual(run.lifecycle, TestRunLifecycle.queued)

        # ...then the third reclaim retires it as a poison pill.
        run.lifecycle = TestRunLifecycle.running
        run.worker_id = w.id
        requeued, retired = reclaim_orphaned_runs(self.s, w.id, _NOW, 2)
        self.s.commit()
        self.assertEqual((requeued, retired), (0, 1))

        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.timed_out)
        self.assertEqual(run.result_code, TestResultCode.ERROR)
        self.assertIsNone(run.worker_id)
        self.assertTrue(run.details.get("reclaim_exhausted"))
        self.assertEqual(run.details.get("reclaim_count"), 3)

        # The parent matrix run must now be complete with a verdict, not
        # wedged open forever.
        from opp_ci.db.models import TestMatrixRun
        mr = self.s.get(TestMatrixRun, mr_id)
        self.assertIsNotNone(mr.completed_at)
        self.assertIsNotNone(mr.verdict)
        self.assertEqual(mr.error_count, 1)

    def test_finalize_promotes_timed_out_run(self):
        # Regression guard for the relaxed lifecycle check: a timed_out run
        # carrying a result_code must promote its pending verdict cell.
        test = get_or_create_test(self.s, _coord())
        mtx = create_matrix_from_axes(self.s, project="mm1k", config={})
        mr = create_matrix_run(self.s, matrix_id=mtx.id)
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=mr.id)
        cell = create_test_verdict(self.s, matrix_run_id=mr.id,
                                   test_id=test.id, test_run_id=run.id)
        run.lifecycle = TestRunLifecycle.timed_out
        run.result_code = TestResultCode.ERROR
        run.finished_at = _NOW
        self.s.flush()

        finalize_verdict_for_run(self.s, run.id)

        self.s.refresh(cell)
        self.assertIsNotNone(cell.verdict)


if __name__ == "__main__":
    unittest.main()
