"""Tests for the per-Test / per-matrix worker selector (routing constraint).

Covers:
  * normalise_worker_selector — cleaning, de-dup, sort; raw tags kept verbatim
    (the worker-name → worker:<name> sugar lives at the input boundary)
  * worker_effective_tags — the implicit worker:<name> tag
  * required_tags_for_run — capability tags ∪ the run's worker_selector
  * web.api._worker_can_run — a run pinned to one worker is claimable only by
    that worker, even when others satisfy every capability tag
  * scheduler._build_matrix_config / expand_matrix — the selector rides on
    every cell and is NOT a cross-product axis (cell count unchanged)
  * expiry — a typo'd worker:<name> is retired unserviceable; a real but
    offline target is left queued

Run with: python -m unittest tests.test_worker_selector   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_wsel_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["OPP_CI_REMOTE"] = "0"

from opp_ci.db.connection import get_engine, SessionLocal        # noqa: E402
from opp_ci.db.models import Base, TestRunLifecycle, Worker      # noqa: E402

engine = get_engine()
from opp_ci.persistence import (                                 # noqa: E402
    create_test_run, expire_unserviceable_queued_runs,
    get_or_create_test, normalise_worker_selector,
    required_tags_for_run, worker_effective_tags,
)
from opp_ci.scheduler import _build_matrix_config, expand_matrix  # noqa: E402
from opp_ci.web.api import _worker_can_run                        # noqa: E402

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


class NormaliseTests(unittest.TestCase):
    def test_empty_is_none(self):
        for empty in (None, "", [], ["", "  "]):
            self.assertIsNone(normalise_worker_selector(empty))

    def test_string_is_wrapped(self):
        self.assertEqual(normalise_worker_selector("gpu"), ["gpu"])

    def test_sorted_and_deduped_verbatim(self):
        # Raw tags are NOT prefixed — "gpu" must not become "worker:gpu".
        self.assertEqual(
            normalise_worker_selector(["worker:b", "gpu", "worker:b", " a "]),
            ["a", "gpu", "worker:b"])


class EffectiveAndRequiredTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def test_effective_tags_include_worker_name(self):
        w = Worker(name="bigbox", token="t", tags=["podman", "gpu"])
        self.assertEqual(worker_effective_tags(w),
                         {"podman", "gpu", "worker:bigbox"})

    def test_required_tags_union_selector(self):
        test = get_or_create_test(self.s, _coord(isolation="podman"))
        run = create_test_run(self.s, test_id=test.id,
                              worker_selector=["worker:bigbox"])
        # capability {"podman"} ∪ selector {"worker:bigbox"}
        self.assertEqual(required_tags_for_run(run),
                         {"podman", "worker:bigbox"})

    def test_no_selector_is_capability_only(self):
        test = get_or_create_test(self.s, _coord(isolation="podman"))
        run = create_test_run(self.s, test_id=test.id)
        self.assertIsNone(run.worker_selector)
        self.assertEqual(required_tags_for_run(run), {"podman"})


class PollMatchingTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _run(self, selector):
        test = get_or_create_test(self.s, _coord(isolation="podman"))
        return create_test_run(self.s, test_id=test.id, worker_selector=selector)

    def test_pin_to_named_worker(self):
        bigbox = Worker(name="bigbox", token="t1", tags=["podman"])
        other = Worker(name="other", token="t2", tags=["podman"])
        run = self._run(["worker:bigbox"])
        # bigbox matches via its implicit worker:bigbox tag...
        self.assertTrue(_worker_can_run(bigbox, run))
        # ...other satisfies podman but not the pin, so it cannot claim it.
        self.assertFalse(_worker_can_run(other, run))

    def test_restrict_by_custom_label(self):
        gpu = Worker(name="gpu1", token="t1", tags=["podman", "gpu"])
        plain = Worker(name="plain", token="t2", tags=["podman"])
        run = self._run(["gpu"])
        self.assertTrue(_worker_can_run(gpu, run))
        self.assertFalse(_worker_can_run(plain, run))

    def test_selector_and_run_filter_both_gate(self):
        # bigbox matches the pin but opts out of podman via a run-filter:
        # willingness is ANDed with the selector, so it must not claim it.
        bigbox = Worker(name="bigbox", token="t", tags=["podman"],
                        run_filters={"isolation": {"deny": ["podman"]}})
        run = self._run(["worker:bigbox"])
        self.assertFalse(_worker_can_run(bigbox, run))


class ExpansionTests(unittest.TestCase):
    def test_build_config_from_workers_and_tags(self):
        cfg = _build_matrix_config(project="inet", kinds="smoke",
                                   workers="bigbox,fastbox", worker_tags="gpu")
        self.assertEqual(cfg["worker_selector"],
                         ["gpu", "worker:bigbox", "worker:fastbox"])

    def test_no_selector_key_when_unset(self):
        cfg = _build_matrix_config(project="inet", kinds="smoke")
        self.assertNotIn("worker_selector", cfg)

    def test_selector_rides_every_cell_not_multiplied(self):
        # Two kinds × two arches = 4 cells; the selector must appear on each,
        # identically, without changing the cell count.
        base = _build_matrix_config(project="inet", kinds="smoke,build",
                                    arches="amd64,aarch64")
        sel = _build_matrix_config(project="inet", kinds="smoke,build",
                                   arches="amd64,aarch64", workers="bigbox")
        self.assertEqual(len(expand_matrix("inet", base)),
                         len(expand_matrix("inet", sel)))
        jobs = expand_matrix("inet", sel)
        self.assertTrue(all(j["worker_selector"] == ["worker:bigbox"] for j in jobs))


class ExpirySelectorTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _queued(self, selector):
        test = get_or_create_test(self.s, _coord(isolation="podman"))
        run = create_test_run(self.s, test_id=test.id, worker_selector=selector)
        run.created_at = _OLD
        self.s.flush()
        return run

    def test_typo_selector_is_unserviceable(self):
        self.s.add(Worker(name="bigbox", token="t", tags=["podman"],
                          status="online", enabled=True, last_heartbeat=_NOW))
        self.s.flush()
        run = self._queued(["worker:typo"])  # no such worker
        expired = expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)
        self.assertEqual(expired, 1)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.timed_out)
        self.assertIn("worker:typo", run.details.get("required_tags"))

    def test_real_but_offline_target_left_queued(self):
        # bigbox exists (enabled) but is offline; its implicit worker:bigbox
        # tag still covers the pinned run, so it must NOT be expired.
        w = Worker(name="bigbox", token="t", tags=["podman"],
                   status="offline", enabled=True)
        self.s.add(w)
        self.s.flush()
        run = self._queued(["worker:bigbox"])
        expired = expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)
        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)


if __name__ == "__main__":
    unittest.main()
