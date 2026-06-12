"""Tests for admin worker management: update concurrency/tags, enable/disable,
and hard delete, across the REST API, the OppCiClient, and the persistence
helpers.

Three layers, mirroring test_remote_cli.py:

1. REST endpoints — FastAPI TestClient against a fresh sqlite DB, asserting
   response shape and role enforcement.
2. OppCiClient methods — `_session.request` mocked so each method's
   verb / URL / payload is checked without a live server.
3. persistence.{update_worker, delete_worker} + Worker.is_available — direct
   against the DB, including reclaim of a running run on delete.

Run with: python -m unittest tests.test_worker_admin   (no pytest needed)

The DB url must be set before importing opp_ci.db, so this module pokes
os.environ at import time.
"""

import datetime
import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_wadmin_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"

from fastapi import FastAPI                                       # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from opp_ci.db.connection import engine, SessionLocal            # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    ApiToken, Base, TestRunLifecycle, Worker,
)
from opp_ci.persistence import (                                 # noqa: E402
    create_test_run, delete_worker, get_or_create_test, update_worker,
)
from opp_ci.web.api import router                                # noqa: E402

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mint_token(role, name=None):
    session = SessionLocal()
    try:
        tok = ApiToken(name=name or f"{role}-tok", role=role)
        session.add(tok)
        session.commit()
        return tok.token
    finally:
        session.close()


def _coord(**over):
    base = {"project": "mm1k", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


class RestWorkerAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cls.client = TestClient(_make_app())
        cls.admin = _mint_token("admin")
        cls.submitter = _mint_token("submitter")

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _register(self, name):
        r = self.client.post("/api/workers/register",
                             json={"name": name, "tags": ["os:linux"], "concurrency": 1},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_update_worker_fields(self):
        wid = self._register("w-update")
        r = self.client.patch(f"/api/workers/{wid}",
                             json={"concurrency": 4, "tags": ["os:linux", "podman"],
                                   "enabled": False},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["concurrency"], 4)
        self.assertEqual(body["tags"], ["os:linux", "podman"])
        self.assertFalse(body["enabled"])

        # the listing reflects the new values + carries the enabled flag
        listing = self.client.get("/api/workers", headers=self._h(self.admin)).json()
        match = [w for w in listing if w["id"] == wid][0]
        self.assertEqual(match["concurrency"], 4)
        self.assertFalse(match["enabled"])

    def test_partial_update_leaves_other_fields(self):
        wid = self._register("w-partial")
        # only concurrency; tags/enabled untouched
        r = self.client.patch(f"/api/workers/{wid}", json={"concurrency": 2},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["tags"], ["os:linux"])
        self.assertTrue(r.json()["enabled"])

    def test_update_invalid_concurrency(self):
        wid = self._register("w-bad")
        r = self.client.patch(f"/api/workers/{wid}", json={"concurrency": 0},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 400, r.text)

    def test_update_unknown_404(self):
        r = self.client.patch("/api/workers/999999", json={"concurrency": 2},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 404)

    def test_role_enforcement(self):
        wid = self._register("w-role")
        r = self.client.patch(f"/api/workers/{wid}", json={"enabled": False},
                             headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 403)
        r = self.client.delete(f"/api/workers/{wid}", headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 403)

    def test_delete_worker(self):
        wid = self._register("w-delete")
        r = self.client.delete(f"/api/workers/{wid}", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 204, r.text)

        listing = self.client.get("/api/workers", headers=self._h(self.admin)).json()
        self.assertFalse(any(w["id"] == wid for w in listing))

        # 404 on unknown
        r = self.client.delete("/api/workers/999999", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 404)


class ShutdownDirectiveTests(unittest.TestCase):
    """A shutdown_requested worker is told to stop via poll & heartbeat, takes
    no new work, and clears the flag when it re-registers (fetches /me)."""

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cls.client = TestClient(_make_app())
        cls.admin = _mint_token("admin")

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _register(self, name):
        r = self.client.post("/api/workers/register",
                             json={"name": name, "tags": ["os:linux"], "concurrency": 1},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        wid = r.json()["id"]
        session = SessionLocal()
        try:
            token = session.get(Worker, wid).token
        finally:
            session.close()
        return wid, token

    def _set_shutdown(self, wid):
        session = SessionLocal()
        try:
            update_worker(session, wid, shutdown_requested=True)
            session.commit()
        finally:
            session.close()

    def test_poll_returns_shutdown_and_no_job(self):
        wid, wtok = self._register("w-shutdown-poll")
        self._set_shutdown(wid)
        r = self.client.post("/api/workers/poll", headers=self._h(wtok))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["command"], "shutdown")
        self.assertIsNone(body["job"])

    def test_heartbeat_relays_shutdown(self):
        wid, wtok = self._register("w-shutdown-hb")
        # No directive while the flag is unset.
        r = self.client.post("/api/workers/heartbeat", json={}, headers=self._h(wtok))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("command", r.json())
        self._set_shutdown(wid)
        r = self.client.post("/api/workers/heartbeat", json={}, headers=self._h(wtok))
        self.assertEqual(r.json().get("command"), "shutdown")

    def test_going_offline_marks_worker_offline(self):
        wid, wtok = self._register("w-goodbye")
        # Come online first (a plain heartbeat bumps last_heartbeat via auth).
        self.client.post("/api/workers/heartbeat", json={}, headers=self._h(wtok))
        session = SessionLocal()
        try:
            w = session.get(Worker, wid)
            self.assertEqual(w.status, "online")
            self.assertIsNotNone(w.last_heartbeat)
        finally:
            session.close()
        # The shutdown goodbye marks it offline immediately.
        r = self.client.post("/api/workers/heartbeat",
                             json={"going_offline": True}, headers=self._h(wtok))
        self.assertEqual(r.status_code, 200, r.text)
        session = SessionLocal()
        try:
            from opp_ci.config import WORKER_HEARTBEAT_TIMEOUT
            w = session.get(Worker, wid)
            self.assertEqual(w.status, "offline")
            # last_heartbeat is back-dated to the staleness threshold rather
            # than wiped, so it reads as offline at once but keeps a roughly
            # accurate last-seen time.
            self.assertIsNotNone(w.last_heartbeat)
            threshold = datetime.datetime.utcnow() - datetime.timedelta(
                seconds=WORKER_HEARTBEAT_TIMEOUT)
            self.assertLessEqual(w.last_heartbeat, threshold)
        finally:
            session.close()

    def test_me_clears_flag(self):
        wid, wtok = self._register("w-shutdown-me")
        self._set_shutdown(wid)
        # Re-registration (a fresh process) clears the flag...
        r = self.client.get("/api/workers/me", headers=self._h(wtok))
        self.assertEqual(r.status_code, 200, r.text)
        session = SessionLocal()
        try:
            self.assertFalse(session.get(Worker, wid).shutdown_requested)
        finally:
            session.close()
        # ...so the next poll hands out work normally (no shutdown loop).
        r = self.client.post("/api/workers/poll", headers=self._h(wtok))
        self.assertNotIn("command", r.json())


class ClientMethodTests(unittest.TestCase):
    """OppCiClient.update_worker / delete_worker hit the right verb/URL/payload."""

    def setUp(self):
        from opp_ci.client import OppCiClient
        self.c = OppCiClient(url="https://ci.example/api", token="t")

    def _patch(self, status=200, json_body=None, content=b"{}"):
        resp = mock.Mock()
        resp.ok = 200 <= status < 300
        resp.status_code = status
        resp.content = content
        resp.json.return_value = json_body if json_body is not None else {}
        resp.text = ""
        return mock.patch.object(self.c._session, "request", return_value=resp)

    def test_update_worker_patch(self):
        with self._patch(json_body={"id": 3}) as req:
            self.c.update_worker(3, concurrency=2, enabled=False)
            self.assertEqual(req.call_args[0][0], "PATCH")
            self.assertEqual(req.call_args[0][1], "https://ci.example/api/workers/3")
            self.assertEqual(req.call_args[1]["json"],
                             {"concurrency": 2, "enabled": False})

    def test_update_worker_omits_unset(self):
        with self._patch(json_body={"id": 3}) as req:
            self.c.update_worker(3, tags=["a", "b"])
            self.assertEqual(req.call_args[1]["json"], {"tags": ["a", "b"]})

    def test_delete_worker(self):
        with self._patch(status=204, content=b"") as req:
            self.assertIsNone(self.c.delete_worker(7))
            self.assertEqual(req.call_args[0][0], "DELETE")
            self.assertEqual(req.call_args[0][1], "https://ci.example/api/workers/7")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _worker(self, **over):
        kw = dict(name="w1", token="tok-w1", concurrency=1, status="online",
                  current_job_count=0, enabled=True)
        kw.update(over)
        w = Worker(**kw)
        self.s.add(w)
        self.s.flush()
        return w

    def test_disabled_worker_not_available(self):
        w = self._worker()
        self.assertTrue(w.is_available)
        w.enabled = False
        self.assertFalse(w.is_available)

    def test_update_worker_rejects_bad_concurrency(self):
        w = self._worker()
        with self.assertRaises(ValueError):
            update_worker(self.s, w.id, concurrency=0)

    def test_update_worker_unknown_returns_none(self):
        self.assertIsNone(update_worker(self.s, 999999, enabled=False))

    def test_delete_worker_reclaims_running_run(self):
        w = self._worker(current_job_count=1)
        test = get_or_create_test(self.s, _coord())
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=None)
        run.lifecycle = TestRunLifecycle.running
        run.worker_id = w.id
        run.started_at = _NOW
        self.s.flush()

        result = delete_worker(self.s, w.id, _NOW, max_reclaims=2)
        self.s.commit()
        self.assertEqual(result, (1, 0))

        # worker is gone, the run is re-queued and detached
        self.assertIsNone(
            self.s.get(Worker, w.id))
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)
        self.assertIsNone(run.worker_id)

    def test_delete_worker_unknown_returns_none(self):
        self.assertIsNone(delete_worker(self.s, 999999, _NOW, max_reclaims=2))


if __name__ == "__main__":
    unittest.main()
