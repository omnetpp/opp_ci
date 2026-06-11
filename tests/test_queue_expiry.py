"""Tests for unserviceable-queued-run expiry.

Covers persistence.expire_unserviceable_queued_runs and required_tags_for_test:
  * a queued run whose required tags no enabled worker advertises is retired
    to timed_out/ERROR once it is older than the timeout, and its parent
    TestMatrixRun completes instead of hanging open
  * a queued run that IS serviceable (an enabled worker's tags cover it,
    even if that worker is offline) is left queued
  * a disabled worker does not count toward serviceability
  * a recently-queued run is left alone until it crosses the timeout
  * timeout <= 0 disables the sweep

Run with: python -m unittest tests.test_queue_expiry   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_qexpiry_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["OPP_CI_REMOTE"] = "0"

from opp_ci.db.connection import engine, SessionLocal            # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    Base, TestMatrixRun, TestResultCode, TestRunLifecycle, Worker,
)
from opp_ci.persistence import (                                 # noqa: E402
    create_matrix_from_axes, create_matrix_run, create_test_run,
    create_test_verdict, expire_unserviceable_queued_runs,
    get_or_create_test, required_tags_for_test,
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


class QueueExpiryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _worker(self, *, tags, status="online", enabled=True, name="w1"):
        w = Worker(name=name, token="tok-" + name, tags=tags,
                   status=status, enabled=enabled, last_heartbeat=_NOW)
        self.s.add(w)
        self.s.flush()
        return w

    def _fleet(self):
        return self.s.query(Worker).all()

    def _queued_run(self, *, coord_over=None, created_at=_OLD, in_matrix=True):
        test = get_or_create_test(self.s, _coord(**(coord_over or {})))
        matrix_run_id = None
        if in_matrix:
            mtx = create_matrix_from_axes(self.s, project="mm1k", config={})
            mr = create_matrix_run(self.s, matrix_id=mtx.id)
            matrix_run_id = mr.id
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=matrix_run_id)
        if in_matrix:
            create_test_verdict(self.s, matrix_run_id=matrix_run_id,
                                test_id=test.id, test_run_id=run.id)
        run.created_at = created_at  # create_test_run stamps "now"; override
        self.s.flush()
        return run, matrix_run_id

    # ── unserviceable expiry ───────────────────────────────────────────

    def test_unserviceable_expired_and_matrix_completes(self):
        self._worker(tags=["linux", "nix"])  # no `podman`
        run, mr_id = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(self.s, _NOW, self._fleet(), 300)
        self.s.commit()

        self.assertEqual(expired, 1)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.timed_out)
        self.assertEqual(run.result_code, TestResultCode.ERROR)
        self.assertIsNone(run.worker_id)
        self.assertTrue(run.details.get("unserviceable"))
        self.assertEqual(run.details.get("required_tags"), ["podman"])

        mr = self.s.get(TestMatrixRun, mr_id)
        self.assertIsNotNone(mr.completed_at)
        self.assertIsNotNone(mr.verdict)
        self.assertEqual(mr.error_count, 1)

    def test_serviceable_by_offline_worker_left_alone(self):
        # The matching worker is offline (not heartbeating) but enabled —
        # it still "covers" the run, so the run must NOT be expired.
        self._worker(tags=["podman"], status="offline")
        run, _ = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(self.s, _NOW, self._fleet(), 300)

        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)

    def test_disabled_worker_does_not_count(self):
        # A disabled worker is draining and takes no jobs, so its tags do not
        # make a run serviceable.
        self._worker(tags=["podman"], enabled=False)
        run, _ = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(self.s, _NOW, self._fleet(), 300)

        self.assertEqual(expired, 1)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.timed_out)

    def test_recent_run_left_alone_until_timeout(self):
        self._worker(tags=["linux", "nix"])  # no `podman`
        run, _ = self._queued_run(
            coord_over={"isolation": "podman"},
            created_at=_NOW - datetime.timedelta(seconds=60))

        expired = expire_unserviceable_queued_runs(self.s, _NOW, self._fleet(), 300)

        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)

    def test_timeout_zero_disables_sweep(self):
        self._worker(tags=["linux"])  # no `podman`
        run, _ = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(self.s, _NOW, self._fleet(), 0)

        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)

    def test_required_tags_for_test_podman(self):
        test = get_or_create_test(self.s, _coord(isolation="podman"))
        self.assertEqual(required_tags_for_test(test), {"podman"})


if __name__ == "__main__":
    unittest.main()
