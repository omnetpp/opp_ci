"""Tests for deleting TestRuns and TestMatrixRuns.

Covers:
  * persistence.delete_test_run — running guard, verdict cascade, rollup refresh
  * persistence.delete_matrix_run — cascade to child runs, cache-hit detach,
    running-child refusal
  * REST DELETE /runs/{id} and /matrix-runs/{id} — permissions (submitter),
    404, 409-on-running

Run with: python -m unittest tests.test_delete_runs   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_del_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402

from opp_ci.db.connection import engine, SessionLocal         # noqa: E402
from opp_ci.db.models import (                                # noqa: E402
    ApiToken, Base, TestMatrix, TestMatrixRun, TestResultCode, TestRun,
    TestRunLifecycle, TestVerdict,
)
from opp_ci.persistence import (                              # noqa: E402
    CannotDeleteRunningRun, create_matrix_run, create_test_run,
    create_test_verdict, delete_matrix_run, delete_test_run,
    get_or_create_test,
)
from opp_ci.web.api import router                             # noqa: E402


def _coord(**over):
    base = {"project": "inet", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


def _matrix(s, project="inet"):
    m = TestMatrix(project=project, config={})
    s.add(m)
    s.flush()
    return m


def _finished_run(s, *, test_id, matrix_run_id, code=TestResultCode.PASS):
    run = create_test_run(s, test_id=test_id, matrix_run_id=matrix_run_id)
    run.lifecycle = TestRunLifecycle.finished
    run.result_code = code
    run.finished_at = datetime.datetime(2026, 1, 1)
    s.flush()
    return run


class DeleteTestRunTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_missing_returns_none(self):
        s = SessionLocal()
        try:
            self.assertIsNone(delete_test_run(s, 99999))
        finally:
            s.close()

    def test_running_run_refused(self):
        s = SessionLocal()
        try:
            t = get_or_create_test(s, _coord())
            run = create_test_run(s, test_id=t.id)
            run.lifecycle = TestRunLifecycle.running
            s.commit()
            with self.assertRaises(CannotDeleteRunningRun):
                delete_test_run(s, run.id)
        finally:
            s.close()

    def test_deletes_verdicts_and_refreshes_rollup(self):
        s = SessionLocal()
        try:
            m = _matrix(s)
            mr = create_matrix_run(s, matrix_id=m.id)
            t = get_or_create_test(s, _coord())
            run = _finished_run(s, test_id=t.id, matrix_run_id=mr.id)
            create_test_verdict(s, matrix_run_id=mr.id, test_id=t.id,
                                test_run_id=run.id,
                                recorded_at=datetime.datetime(2026, 1, 1))
            from opp_ci.persistence import recompute_matrix_run_rollup
            recompute_matrix_run_rollup(s, mr.id)
            s.commit()
            self.assertEqual(s.get(TestMatrixRun, mr.id).total_count, 1)

            delete_test_run(s, run.id)
            s.commit()

            self.assertIsNone(s.get(TestRun, run.id))
            self.assertEqual(
                s.query(TestVerdict).filter_by(test_run_id=run.id).count(), 0)
            # rollup recomputed to reflect the now-empty matrix run
            self.assertEqual(s.get(TestMatrixRun, mr.id).total_count, 0)
        finally:
            s.close()


class DeleteMatrixRunTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_missing_returns_none(self):
        s = SessionLocal()
        try:
            self.assertIsNone(delete_matrix_run(s, 99999))
        finally:
            s.close()

    def test_cascade_deletes_exclusive_children(self):
        s = SessionLocal()
        try:
            m = _matrix(s)
            mr = create_matrix_run(s, matrix_id=m.id)
            t = get_or_create_test(s, _coord())
            run = _finished_run(s, test_id=t.id, matrix_run_id=mr.id)
            create_test_verdict(s, matrix_run_id=mr.id, test_id=t.id,
                                test_run_id=run.id,
                                recorded_at=datetime.datetime(2026, 1, 1))
            s.commit()
            run_id, mr_id = run.id, mr.id

            result = delete_matrix_run(s, mr_id)
            s.commit()

            self.assertEqual(result, {"deleted_runs": 1, "detached_runs": 0})
            self.assertIsNone(s.get(TestMatrixRun, mr_id))
            self.assertIsNone(s.get(TestRun, run_id))
            self.assertEqual(
                s.query(TestVerdict).filter_by(matrix_run_id=mr_id).count(), 0)
        finally:
            s.close()

    def test_cache_hit_child_is_detached_not_deleted(self):
        """A child run of A reused by B's cache-hit cell must survive A's
        deletion (detached, matrix_run_id NULL), and B's cell must keep it."""
        s = SessionLocal()
        try:
            m = _matrix(s)
            t = get_or_create_test(s, _coord())

            mr_a = create_matrix_run(s, matrix_id=m.id)
            run = _finished_run(s, test_id=t.id, matrix_run_id=mr_a.id)
            create_test_verdict(s, matrix_run_id=mr_a.id, test_id=t.id,
                                test_run_id=run.id,
                                recorded_at=datetime.datetime(2026, 1, 1))

            # B reuses the same finished run via a cache hit.
            mr_b = create_matrix_run(s, matrix_id=m.id)
            create_test_verdict(s, matrix_run_id=mr_b.id, test_id=t.id,
                                test_run_id=run.id, cache_hit=True,
                                recorded_at=datetime.datetime(2026, 1, 2))
            s.commit()
            run_id, mr_a_id, mr_b_id = run.id, mr_a.id, mr_b.id

            result = delete_matrix_run(s, mr_a_id)
            s.commit()

            self.assertEqual(result, {"deleted_runs": 0, "detached_runs": 1})
            self.assertIsNone(s.get(TestMatrixRun, mr_a_id))
            kept = s.get(TestRun, run_id)
            self.assertIsNotNone(kept)               # run survived
            self.assertIsNone(kept.matrix_run_id)    # detached from A
            # B and its cell are intact
            self.assertIsNotNone(s.get(TestMatrixRun, mr_b_id))
            self.assertEqual(
                s.query(TestVerdict).filter_by(matrix_run_id=mr_b_id).count(), 1)
        finally:
            s.close()

    def test_running_child_refuses_whole_delete(self):
        s = SessionLocal()
        try:
            m = _matrix(s)
            mr = create_matrix_run(s, matrix_id=m.id)
            t = get_or_create_test(s, _coord())
            run = create_test_run(s, test_id=t.id, matrix_run_id=mr.id)
            run.lifecycle = TestRunLifecycle.running
            s.commit()
            mr_id, run_id = mr.id, run.id

            with self.assertRaises(CannotDeleteRunningRun):
                delete_matrix_run(s, mr_id)
            s.rollback()

            # nothing removed
            self.assertIsNotNone(s.get(TestMatrixRun, mr_id))
            self.assertIsNotNone(s.get(TestRun, run_id))
        finally:
            s.close()


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mint(role):
    s = SessionLocal()
    try:
        tok = ApiToken(name=f"{role}-del", role=role)
        s.add(tok)
        s.commit()
        return tok.token
    finally:
        s.close()


class RestDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cls.client = TestClient(_make_app())
        cls.readonly = _mint("readonly")
        cls.submitter = _mint("submitter")

    def _h(self, tok):
        return {"Authorization": f"Bearer {tok}"}

    def test_submitter_can_delete_run(self):
        r = self.client.post("/api/runs", headers=self._h(self.submitter),
                             json={"project": "inet", "kind": "smoke"})
        run_id = r.json()["id"]

        # readonly still forbidden
        r = self.client.delete(f"/api/runs/{run_id}",
                               headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 403)

        # submitter now allowed (was admin-only)
        r = self.client.delete(f"/api/runs/{run_id}",
                               headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 204)
        r = self.client.get(f"/api/runs/{run_id}", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 404)

    def test_delete_running_run_409(self):
        r = self.client.post("/api/runs", headers=self._h(self.submitter),
                             json={"project": "inet", "kind": "smoke"})
        run_id = r.json()["id"]
        s = SessionLocal()
        try:
            s.get(TestRun, run_id).lifecycle = TestRunLifecycle.running
            s.commit()
        finally:
            s.close()
        r = self.client.delete(f"/api/runs/{run_id}",
                               headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 409, r.text)

    def test_delete_matrix_run_endpoint(self):
        r = self.client.post("/api/matrix-runs", headers=self._h(self.submitter),
                             json={"project": "inet", "kinds": ["smoke"]})
        self.assertEqual(r.status_code, 200, r.text)
        mr_id = r.json()["matrix_run_id"]

        # readonly forbidden
        r = self.client.delete(f"/api/matrix-runs/{mr_id}",
                               headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 403)

        # submitter allowed
        r = self.client.delete(f"/api/matrix-runs/{mr_id}",
                               headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 204, r.text)

        # gone
        s = SessionLocal()
        try:
            self.assertIsNone(s.get(TestMatrixRun, mr_id))
        finally:
            s.close()

        # second delete → 404
        r = self.client.delete(f"/api/matrix-runs/{mr_id}",
                               headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 404)

    def test_bulk_delete_submitter_skips_running(self):
        ids = []
        for _ in range(2):
            r = self.client.post("/api/runs", headers=self._h(self.submitter),
                                 json={"project": "bulkproj", "kind": "build"})
            ids.append(r.json()["id"])
        # mark one running — bulk delete must skip it, not abort
        s = SessionLocal()
        try:
            s.get(TestRun, ids[0]).lifecycle = TestRunLifecycle.running
            s.commit()
        finally:
            s.close()

        r = self.client.delete("/api/runs?project=bulkproj&confirm=true",
                               headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["skipped_running"], 1)
        # the running one survives
        r = self.client.get(f"/api/runs/{ids[0]}", headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
