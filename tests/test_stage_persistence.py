"""Tests for persisting captured stages to TestRunStage and rendering the
finished staged view (plan/pending/staged-execution-capture.md, phase 3).

Run with: python -m unittest tests.test_stage_persistence
"""

import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_stp_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"
os.environ.setdefault("OPP_CI_SESSION_SECRET", "x" * 40)

from opp_ci import config as _cfg                                # noqa: E402
_cfg.SESSION_SECRET = _cfg.SESSION_SECRET or "x" * 40

from fastapi.testclient import TestClient                       # noqa: E402
from sqlalchemy import select                                   # noqa: E402

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import (                                  # noqa: E402
    Base, TestRun, TestRunStage, TestRunLifecycle, User, Worker)
from opp_ci.persistence import get_or_create_test, create_test_run  # noqa: E402
from opp_ci.web import app as webapp                            # noqa: E402


def _coord(**over):
    base = {"project": "mm1k", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


def _stages():
    return [
        {"name": "deps.install", "ordinal": 0, "command": "opp_env install",
         "status": "passed", "exit": 0, "started_at": 100.0, "finished_at": 104.0,
         "output": [{"stream": "out", "text": "installing"}]},
        {"name": "project.build", "ordinal": 1, "command": "opp_build_project",
         "status": "passed", "exit": 0, "started_at": 104.0, "finished_at": 110.0,
         "output": [{"stream": "out", "text": "compiling"},
                    {"stream": "err", "text": "warn: x"}]},
        {"name": "test.run", "ordinal": 2, "command": "opp_run_smoke_tests --no-build",
         "status": "passed", "exit": 0, "started_at": 110.0, "finished_at": 112.0,
         "output": [{"stream": "out", "text": "PASS"}]},
    ]


class StagePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        cls._fv = mock.patch("opp_ci.web.api.finalize_verdict_for_run", lambda *a, **k: None)
        cls._fv.start()
        s = SessionLocal()
        try:
            s.add(User(id=8301, username="stp-sub", role="submitter", enabled=True))
            s.add(Worker(id=8310, name="stp-worker", token="tok-stp", status="online"))
            s.commit()
        finally:
            s.close()

        def _load(_uid):
            s = SessionLocal()
            try:
                return s.execute(select(User).where(User.id == 8301)).scalar_one_or_none()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()
        cls._fv.stop()

    def _new_running_run(self):
        s = SessionLocal()
        try:
            test = get_or_create_test(s, _coord())
            run = create_test_run(s, test_id=test.id, matrix_run_id=None)
            run.lifecycle = TestRunLifecycle.running
            run.worker_id = 8310
            s.commit()
            return run.id
        finally:
            s.close()

    def _post_result(self, run_id, stages):
        return self.client.post(
            "/api/workers/result",
            headers={"Authorization": "Bearer tok-stp"},
            json={"run_id": run_id, "result_code": "PASS",
                  "test_exec_seconds": 2.0, "stages": stages})

    def test_result_persists_stages(self):
        run_id = self._new_running_run()
        r = self._post_result(run_id, _stages())
        self.assertEqual(r.status_code, 200)
        s = SessionLocal()
        try:
            rows = s.execute(
                select(TestRunStage).where(TestRunStage.test_run_id == run_id)
                .order_by(TestRunStage.ordinal)).scalars().all()
            self.assertEqual([r.name for r in rows],
                             ["deps.install", "project.build", "test.run"])
            self.assertEqual([r.exit_code for r in rows], [0, 0, 0])
            # duration derived from started/finished timestamps
            self.assertEqual([r.duration_seconds for r in rows], [4.0, 6.0, 2.0])
            # per-line output preserved with stream tags
            self.assertEqual(rows[1].output,
                             [{"stream": "out", "text": "compiling"},
                              {"stream": "err", "text": "warn: x"}])
        finally:
            s.close()

    def test_reposting_replaces_stages(self):
        run_id = self._new_running_run()
        self._post_result(run_id, _stages())
        # Re-report with a single stage → old rows replaced, not appended.
        self._post_result(run_id, [
            {"name": "test.run", "ordinal": 0, "command": "x", "status": "failed",
             "exit": 1, "output": []}])
        s = SessionLocal()
        try:
            rows = s.execute(
                select(TestRunStage).where(TestRunStage.test_run_id == run_id)).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "failed")
        finally:
            s.close()

    def test_run_detail_renders_stages(self):
        run_id = self._new_running_run()
        self._post_result(run_id, _stages())
        html = self.client.get(f"/test-runs/{run_id}").text
        self.assertIn("Stages", html)
        self.assertIn("deps.install", html)
        self.assertIn("project.build", html)
        self.assertIn("opp_build_project", html)   # command shown

    def test_cascade_delete_removes_stages(self):
        run_id = self._new_running_run()
        self._post_result(run_id, _stages())
        s = SessionLocal()
        try:
            run = s.get(TestRun, run_id)
            s.delete(run)
            s.commit()
            remaining = s.execute(
                select(TestRunStage).where(TestRunStage.test_run_id == run_id)).scalars().all()
            self.assertEqual(remaining, [])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
