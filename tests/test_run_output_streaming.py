"""Tests for live per-run test-output streaming (plan/pending/
remote-worker-log-view.md, feature 2).

Covers:
  * run_output.RunOutputStore — serve seq assignment, since(after_seq)
    slicing, has()/drop(), per-run ring bound, LRU eviction of whole runs
  * executor._CallbackStringIO — getvalue() preserved, per-line callback,
    partial line buffered
  * executor on_output wiring — streamed external output tees each line;
    run_test forwards on_output to the chosen helper
  * worker._RunOutputStreamer — batches and ships lines, swallows POST
    failures, final flush on stop
  * POST /api/runs/{id}/output-append — ownership gating (assigned worker
    stores; wrong worker dropped; unknown run 404)
  * GET /test-runs/{id}/output/tail — entries/cursor round-trip, html
    escaping, done flag flips with lifecycle

Run with: python -m unittest tests.test_run_output_streaming
"""

import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_rout_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"
os.environ.setdefault("OPP_CI_SESSION_SECRET", "x" * 40)

from opp_ci import config as _cfg                                # noqa: E402
_cfg.SESSION_SECRET = _cfg.SESSION_SECRET or "x" * 40

from fastapi.testclient import TestClient                       # noqa: E402
from sqlalchemy import select                                   # noqa: E402

from opp_ci import executor                                     # noqa: E402
from opp_ci.executor import run_external, _CallbackStringIO     # noqa: E402
from opp_ci.run_output import RunOutputStore, STORE             # noqa: E402
from opp_ci.worker import _RunOutputStreamer                    # noqa: E402
from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import (                                  # noqa: E402
    Base, TestRunLifecycle, User, Worker)
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


class RunOutputStoreTests(unittest.TestCase):
    def test_append_assigns_seqs_and_since(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(1, ["a", "b"])
        entries, last = s.since(1, 0)
        self.assertEqual([e["seq"] for e in entries], [1, 2])
        self.assertEqual([e["text"] for e in entries], ["a", "b"])
        self.assertEqual(last, 2)
        # exclusive on after_seq
        entries, last = s.since(1, 1)
        self.assertEqual([e["text"] for e in entries], ["b"])

    def test_since_empty_keeps_cursor(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(1, ["a"])
        entries, last = s.since(1, 9)
        self.assertEqual(entries, [])
        self.assertEqual(last, 9)

    def test_has_and_drop(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(5, ["x"])
        self.assertTrue(s.has(5))
        s.drop(5)
        self.assertFalse(s.has(5))

    def test_per_run_ring_bound(self):
        s = RunOutputStore(ring=2, max_runs=10)
        s.append(1, ["a", "b", "c"])
        entries, _ = s.since(1, 0)
        self.assertEqual([e["text"] for e in entries], ["b", "c"])

    def test_lru_evicts_oldest_run(self):
        s = RunOutputStore(ring=100, max_runs=2)
        s.append(1, ["a"])
        s.append(2, ["b"])
        s.append(1, ["a2"])     # touch run 1 → run 2 is now LRU
        s.append(3, ["c"])      # over cap → evict run 2
        self.assertTrue(s.has(1))
        self.assertFalse(s.has(2))
        self.assertTrue(s.has(3))


class CallbackStringIOTests(unittest.TestCase):
    def test_getvalue_preserved_and_lines_forwarded(self):
        seen = []
        buf = _CallbackStringIO(seen.append)
        buf.write("hello\nwor")
        buf.write("ld\n")
        self.assertEqual(buf.getvalue(), "hello\nworld\n")
        self.assertEqual(seen, ["hello", "world"])

    def test_partial_line_buffered(self):
        seen = []
        buf = _CallbackStringIO(seen.append)
        buf.write("no newline yet")
        self.assertEqual(seen, [])            # nothing forwarded until "\n"
        self.assertEqual(buf.getvalue(), "no newline yet")

    def test_callback_exception_swallowed(self):
        def boom(_):
            raise RuntimeError("nope")
        buf = _CallbackStringIO(boom)
        buf.write("x\n")                      # must not raise
        self.assertEqual(buf.getvalue(), "x\n")


class ExecutorWiringTests(unittest.TestCase):
    def test_streaming_tees_each_line(self):
        seen = []
        result = run_external(
            ["python", "-c", "print('a'); print('b')"],
            label="t", stream=True, on_output=seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "a\nb\n")   # capture unaffected
        self.assertEqual(seen, ["a", "b"])          # teed live

    def test_run_test_forwards_on_output(self):
        captured = {}

        def _fake_direct(project, kind, *, on_output=None, **kw):
            captured["on_output"] = on_output
            return {"result_code": "PASS", "test_exec_seconds": 0.0,
                    "stdout": "", "stderr": "", "details": None, "commit_sha": None}

        cb = lambda line: None
        with mock.patch.object(executor, "_run_test_direct", _fake_direct):
            executor.run_test("mm1k", "smoke", isolation="none", toolchain="none",
                              on_output=cb)
        self.assertIs(captured["on_output"], cb)


class _FakeResp:
    status_code = 200


class _FakeSession:
    def __init__(self, raise_exc=None):
        self.raise_exc = raise_exc
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResp()


class RunOutputStreamerTests(unittest.TestCase):
    def test_flush_batches_pending_lines(self):
        sess = _FakeSession()
        st = _RunOutputStreamer(sess, "http://c", 42)
        st.append("one")
        st.append("two")
        st._flush()
        self.assertEqual(sess.posts[0]["url"], "http://c/api/runs/42/output-append")
        self.assertEqual(sess.posts[0]["json"], {"lines": ["one", "two"]})
        # pending cleared; a second flush with nothing posts nothing
        st._flush()
        self.assertEqual(len(sess.posts), 1)

    def test_flush_failure_swallowed(self):
        import requests
        sess = _FakeSession(raise_exc=requests.RequestException("down"))
        st = _RunOutputStreamer(sess, "http://c", 1)
        st.append("x")
        st._flush()                            # must not raise

    def test_stop_does_final_flush(self):
        sess = _FakeSession()
        st = _RunOutputStreamer(sess, "http://c", 7)
        st.start()
        st.append("late")
        st.stop()                              # joins thread, then final flush
        all_lines = [l for p in sess.posts for l in p["json"]["lines"]]
        self.assertIn("late", all_lines)


class RunOutputRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8201, username="rout-sub", role="submitter", enabled=True))
            # status=online: an offline worker's first authenticated request
            # reclaims its running runs (clearing worker_id), which is exactly
            # what we're asserting ownership against. A streaming worker is
            # online by then.
            s.add(Worker(id=8210, name="rout-owner", token="tok-rout-own",
                         status="online"))
            s.add(Worker(id=8211, name="rout-other", token="tok-rout-other",
                         status="online"))
            s.commit()
            test = get_or_create_test(s, _coord())
            run = create_test_run(s, test_id=test.id, matrix_run_id=None)
            run.lifecycle = TestRunLifecycle.running
            run.worker_id = 8210
            s.commit()
            cls.run_id = run.id
        finally:
            s.close()

        def _load(_uid):
            s = SessionLocal()
            try:
                return s.execute(
                    select(User).where(User.id == 8201)).scalar_one_or_none()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()
        STORE.drop(cls.run_id)

    def test_append_requires_owning_worker(self):
        STORE.drop(self.run_id)
        # Wrong worker → dropped, nothing stored.
        r = self.client.post(
            f"/api/runs/{self.run_id}/output-append",
            headers={"Authorization": "Bearer tok-rout-other"},
            json={"lines": ["nope"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "dropped")
        self.assertFalse(STORE.has(self.run_id))
        # Owning worker → stored.
        r = self.client.post(
            f"/api/runs/{self.run_id}/output-append",
            headers={"Authorization": "Bearer tok-rout-own"},
            json={"lines": ["hi"]})
        self.assertEqual(r.json()["status"], "ok")
        self.assertTrue(STORE.has(self.run_id))

    def test_append_unknown_run_404(self):
        r = self.client.post(
            "/api/runs/999999/output-append",
            headers={"Authorization": "Bearer tok-rout-own"},
            json={"lines": ["x"]})
        self.assertEqual(r.status_code, 404)

    def test_tail_serves_and_done_tracks_lifecycle(self):
        STORE.drop(self.run_id)
        STORE.append(self.run_id, ["<b>line</b>"])
        r = self.client.get(f"/test-runs/{self.run_id}/output/tail")
        data = r.json()
        self.assertTrue(data["available"])
        self.assertFalse(data["done"])                     # still running
        self.assertIn("&lt;b&gt;line&lt;/b&gt;", data["entries"][0]["html"])
        # re-poll with the cursor → nothing new
        r2 = self.client.get(
            f"/test-runs/{self.run_id}/output/tail?cursor={data['cursor']}")
        self.assertEqual(r2.json()["entries"], [])
        # flip lifecycle terminal → done true
        s = SessionLocal()
        try:
            run = s.execute(
                select(webapp.TestRun).where(webapp.TestRun.id == self.run_id)
            ).scalar_one()
            run.lifecycle = TestRunLifecycle.finished
            s.commit()
        finally:
            s.close()
        self.assertTrue(
            self.client.get(f"/test-runs/{self.run_id}/output/tail").json()["done"])


if __name__ == "__main__":
    unittest.main()
