"""Tests for remote-worker log shipping (plan/pending/remote-worker-log-view.md,
feature 1).

Covers:
  * logbuffer.RingBufferHandler — capture, ordering, monotonic seq, bound/
    eviction, since(after_seq) slicing, drop-count when over the limit
  * worker_logs.WorkerLogStore — serve-side seq assignment, since(), has()
  * WorkerAgent log shipping — batches new-since-last, advances the
    watermark only on a 200, re-ships on failure, inserts a drop marker,
    sends no body when nothing is new
  * coordinator heartbeat ingests the logs field (and old no-body workers
    still work)
  * worker_log_tail source selection (shipped store vs journalctl fallback)
    and the render adapter (level→priority, html/ansi escaping)

Run with: python -m unittest tests.test_worker_log_shipping
"""

import logging
import os
import tempfile
import types
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_wlog_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"
os.environ.setdefault("OPP_CI_SESSION_SECRET", "x" * 40)

from opp_ci import config as _cfg                                # noqa: E402
_cfg.SESSION_SECRET = _cfg.SESSION_SECRET or "x" * 40

from fastapi.testclient import TestClient                       # noqa: E402
from sqlalchemy import select                                   # noqa: E402

from opp_ci.logbuffer import RingBufferHandler                  # noqa: E402
from opp_ci.worker_logs import WorkerLogStore, STORE            # noqa: E402
from opp_ci.worker import WorkerAgent                           # noqa: E402
from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, User, Worker                 # noqa: E402
from opp_ci.web import app as webapp                            # noqa: E402


def _rec(handler, msg, level=logging.INFO):
    """Push one record through a handler without a real logger tree."""
    handler.emit(logging.LogRecord(
        "opp_ci.test", level, __file__, 0, msg, None, None))


class RingBufferHandlerTests(unittest.TestCase):
    def test_capture_order_and_seq(self):
        h = RingBufferHandler(capacity=10)
        for m in ["a", "b", "c"]:
            _rec(h, m)
        entries, dropped = h.since(0)
        self.assertEqual([e["msg"] for e in entries], ["a", "b", "c"])
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3])
        self.assertEqual(dropped, 0)

    def test_since_is_exclusive(self):
        h = RingBufferHandler(capacity=10)
        for m in ["a", "b", "c"]:
            _rec(h, m)
        entries, _ = h.since(2)
        self.assertEqual([e["msg"] for e in entries], ["c"])

    def test_bound_evicts_oldest(self):
        h = RingBufferHandler(capacity=2)
        for m in ["a", "b", "c"]:
            _rec(h, m)
        entries, _ = h.since(0)
        # 'a' (seq 1) evicted; seqs stay monotonic across eviction.
        self.assertEqual([e["msg"] for e in entries], ["b", "c"])
        self.assertEqual([e["seq"] for e in entries], [2, 3])

    def test_limit_reports_dropped_keeps_recent(self):
        h = RingBufferHandler(capacity=10)
        for m in ["a", "b", "c", "d"]:
            _rec(h, m)
        entries, dropped = h.since(0, limit=2)
        self.assertEqual([e["msg"] for e in entries], ["c", "d"])
        self.assertEqual(dropped, 2)

    def test_level_and_ts_captured(self):
        h = RingBufferHandler(capacity=10)
        _rec(h, "boom", level=logging.ERROR)
        e = h.since(0)[0][0]
        self.assertEqual(e["level"], logging.ERROR)
        self.assertIsInstance(e["ts"], float)


class WorkerLogStoreTests(unittest.TestCase):
    def test_append_assigns_serve_seqs(self):
        s = WorkerLogStore(capacity=10)
        s.append(1, [{"ts": 1.0, "level": 20, "msg": "x"},
                     {"ts": 2.0, "level": 20, "msg": "y"}])
        entries, last = s.since(1, 0)
        self.assertEqual([e["seq"] for e in entries], [1, 2])
        self.assertEqual([e["msg"] for e in entries], ["x", "y"])
        self.assertEqual(last, 2)

    def test_since_empty_keeps_cursor(self):
        s = WorkerLogStore(capacity=10)
        s.append(1, [{"ts": 1.0, "level": 20, "msg": "x"}])
        entries, last = s.since(1, 5)
        self.assertEqual(entries, [])
        self.assertEqual(last, 5)  # never goes backwards

    def test_has_and_isolation_per_worker(self):
        s = WorkerLogStore(capacity=10)
        self.assertFalse(s.has(1))
        s.append(1, [{"ts": 1.0, "level": 20, "msg": "x"}])
        self.assertTrue(s.has(1))
        self.assertFalse(s.has(2))

    def test_bound_per_worker(self):
        s = WorkerLogStore(capacity=2)
        s.append(7, [{"msg": m} for m in ["a", "b", "c"]])
        entries, _ = s.since(7, 0)
        self.assertEqual([e["msg"] for e in entries], ["b", "c"])


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, status_code=200, raise_exc=None):
        self.status_code = status_code
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResp(self.status_code)


class WorkerShippingTests(unittest.TestCase):
    def _agent(self):
        a = WorkerAgent(coordinator_url="http://c", token="t")
        a._log_handler = RingBufferHandler(capacity=100)
        return a

    def test_no_handler_sends_no_logs(self):
        a = WorkerAgent(coordinator_url="http://c", token="t")  # handler None
        sess = _FakeSession()
        a._heartbeat(sess)
        self.assertEqual(sess.calls[0]["json"], None)

    def test_nothing_new_sends_no_body(self):
        a = self._agent()
        sess = _FakeSession()
        a._heartbeat(sess)
        self.assertIsNone(sess.calls[0]["json"])

    def test_ships_new_and_advances_on_200(self):
        a = self._agent()
        for m in ["one", "two"]:
            _rec(a._log_handler, m)
        sess = _FakeSession(status_code=200)
        a._heartbeat(sess)
        body = sess.calls[0]["json"]
        self.assertEqual([e["msg"] for e in body["logs"]["entries"]],
                         ["one", "two"])
        self.assertEqual(a._last_shipped_seq, 2)
        # A second beat with nothing new sends no body.
        a._heartbeat(sess)
        self.assertIsNone(sess.calls[1]["json"])

    def test_failure_does_not_advance_watermark(self):
        a = self._agent()
        _rec(a._log_handler, "one")
        a._heartbeat(_FakeSession(status_code=503))
        self.assertEqual(a._last_shipped_seq, 0)
        # Network error path likewise leaves the watermark untouched.
        import requests
        a._heartbeat(_FakeSession(raise_exc=requests.RequestException("x")))
        self.assertEqual(a._last_shipped_seq, 0)
        # Recovery re-ships the same line.
        sess = _FakeSession(status_code=200)
        a._heartbeat(sess)
        self.assertEqual([e["msg"] for e in sess.calls[0]["json"]["logs"]["entries"]],
                         ["one"])
        self.assertEqual(a._last_shipped_seq, 1)

    def test_drop_marker_inserted_over_batch_cap(self):
        a = self._agent()
        for m in ["a", "b", "c", "d"]:
            _rec(a._log_handler, m)
        sess = _FakeSession(status_code=200)
        with mock.patch.object(_cfg, "WORKER_LOG_BATCH", 2):
            a._heartbeat(sess)
        msgs = [e["msg"] for e in sess.calls[0]["json"]["logs"]["entries"]]
        self.assertIn("dropped", msgs[0])
        self.assertEqual(msgs[1:], ["c", "d"])
        # Watermark advances to the real high seq (4), not the marker.
        self.assertEqual(a._last_shipped_seq, 4)


class LogShippingRouteTests(unittest.TestCase):
    WID_SHIPPED = 8107
    WID_LOCAL = 8108

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8101, username="wlog-sub", role="submitter", enabled=True))
            s.add(Worker(id=cls.WID_SHIPPED, name="remote-1", token="tok-wlog-r"))
            s.add(Worker(id=cls.WID_LOCAL, name="local", token="tok-wlog-l"))
            s.commit()
        finally:
            s.close()

        def _load(_uid):
            s = SessionLocal()
            try:
                return s.execute(
                    select(User).where(User.id == 8101)).scalar_one_or_none()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()

    def test_heartbeat_ingests_logs(self):
        # Authenticated worker heartbeat carrying a logs body lands in STORE.
        r = self.client.post(
            "/api/workers/heartbeat",
            headers={"Authorization": "Bearer tok-wlog-r"},
            json={"logs": {"entries": [
                {"ts": 1700000000.0, "level": logging.INFO, "msg": "hello-ship"}]}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(STORE.has(self.WID_SHIPPED))

    def test_heartbeat_no_body_still_ok(self):
        r = self.client.post(
            "/api/workers/heartbeat",
            headers={"Authorization": "Bearer tok-wlog-l"},
        )
        self.assertEqual(r.status_code, 200)

    def test_tail_serves_shipped_when_present(self):
        STORE.append(self.WID_SHIPPED, [
            {"ts": 1700000000.0, "level": logging.ERROR, "msg": "<x> boom"}])
        # read_unit must NOT be consulted when shipped logs exist.
        with mock.patch("opp_ci.journal.read_unit",
                        side_effect=AssertionError("journalctl should not run")):
            r = self.client.get(f"/logs/worker/{self.WID_SHIPPED}/tail")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["available"])
        row = data["entries"][-1]
        self.assertEqual(row["priority"], 3)            # ERROR → 3
        self.assertIn("&lt;x&gt;", row["html"])         # html-escaped
        self.assertNotIn("<x>", row["html"])
        # cursor is the serve seq; a re-poll with it yields nothing new.
        r2 = self.client.get(
            f"/logs/worker/{self.WID_SHIPPED}/tail?cursor={data['cursor']}")
        self.assertEqual(r2.json()["entries"], [])

    def test_tail_falls_back_to_journalctl(self):
        captured = {}

        def _fake_read(unit, **kw):
            captured["unit"] = unit
            return [], None

        # WID_LOCAL has shipped nothing → journalctl path.
        with mock.patch("opp_ci.journal.read_unit", side_effect=_fake_read):
            r = self.client.get(f"/logs/worker/{self.WID_LOCAL}/tail")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["unit"], "opp_ci-worker@local.service")


if __name__ == "__main__":
    unittest.main()
