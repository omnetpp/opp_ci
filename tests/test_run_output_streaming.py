"""Tests for staged live capture — the worker→coordinator→run-detail pipeline
(plan/pending/staged-execution-capture.md, phase 1). Supersedes the earlier
flat per-run output tests; the model is now stage events.

Covers:
  * executor stream tagging — run_external tees each line with its stream
  * executor._CallbackStringIO — getvalue preserved, per-line callback
  * executor build/test split on the opp_env path (project.build then
    test.run; build failure skips the test)
  * run_output.RunOutputStore — events build stages + lines, snapshot cursor,
    has/drop, LRU run eviction
  * worker._RunOutputStreamer — batches + ships events, swallows failures,
    final flush on stop
  * POST /api/runs/{id}/output-append — ownership gating + 404
  * GET /test-runs/{id}/output/tail — stages + incremental lines + done,
    html escaping, stream tagging

Run with: python -m unittest tests.test_run_output_streaming
"""

import contextlib
import os
import subprocess
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
from opp_ci.stages import Stage, StageRecorder, FAILED, SKIPPED, PASSED  # noqa: E402
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


def _ev(kind, **kw):
    return dict(kind=kind, **kw)


class StreamTaggingTests(unittest.TestCase):
    def test_tees_lines_with_stream(self):
        seen = []
        result = run_external(
            ["python", "-c",
             "import sys; print('to-out'); print('to-err', file=sys.stderr)"],
            label="t", stream=True, on_output=lambda stream, text: seen.append((stream, text)))
        self.assertEqual(result.returncode, 0)
        self.assertIn(("out", "to-out"), seen)
        self.assertIn(("err", "to-err"), seen)


class CallbackStringIOTests(unittest.TestCase):
    def test_getvalue_preserved_and_lines_forwarded(self):
        seen = []
        buf = _CallbackStringIO(seen.append)
        buf.write("hello\nwor")
        buf.write("ld\n")
        self.assertEqual(buf.getvalue(), "hello\nworld\n")
        self.assertEqual(seen, ["hello", "world"])

    def test_callback_exception_swallowed(self):
        def boom(_):
            raise RuntimeError("nope")
        buf = _CallbackStringIO(boom)
        buf.write("x\n")
        self.assertEqual(buf.getvalue(), "x\n")


class OppEnvBuildTestSplitTests(unittest.TestCase):
    def _patches(self, fake_run):
        return [
            mock.patch("opp_ci.executor.resolve_opp_env_id", return_value=("mm1k", None)),
            mock.patch("opp_ci.executor._opp_env_workspace", return_value="/tmp/ws"),
            mock.patch("opp_ci.executor._workspace_lock", lambda ws: contextlib.nullcontext()),
            mock.patch("opp_ci.executor.run_external", side_effect=fake_run),
        ]

    def test_splits_into_build_then_test(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(fake_run):
                es.enter_context(p)
            outcome = executor._run_test_via_opp_env("mm1k", "smoke", recorder=rec)

        self.assertEqual([s["name"] for s in rec.stages],
                         [Stage.PROJECT_BUILD, Stage.TEST_RUN])
        self.assertEqual([s["status"] for s in rec.stages], [PASSED, PASSED])
        self.assertEqual(outcome["result_code"], "PASS")
        self.assertIn("opp_build_project", calls[0][-1])     # build inner cmd
        self.assertIn("--no-build", calls[1][-1])            # test skips rebuild

    def test_build_failure_skips_test(self):
        def fake_run(args, **kw):
            return subprocess.CompletedProcess(args, 2, stdout="boom\n", stderr="err\n")

        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(fake_run):
                es.enter_context(p)
            outcome = executor._run_test_via_opp_env("mm1k", "smoke", recorder=rec)

        self.assertEqual(outcome["result_code"], "FAIL")
        self.assertEqual(rec.stages[0]["name"], Stage.PROJECT_BUILD)
        self.assertEqual(rec.stages[0]["status"], FAILED)
        self.assertEqual(rec.stages[1]["name"], Stage.TEST_RUN)
        self.assertEqual(rec.stages[1]["status"], SKIPPED)


class DirectBuildTestSplitTests(unittest.TestCase):
    """_run_test_direct splits into project.build (simulation_project.build)
    then test.run (the runner with build=False)."""

    def _patches(self, sim, funcs):
        return [
            mock.patch("opp_ci.executor._load_workspace", return_value=(None, sim)),
            mock.patch("opp_ci.executor._get_test_functions", return_value=funcs),
            mock.patch("opp_repl.common.util.ensure_logging_initialized", create=True),
            mock.patch("opp_ci.executor.resolve_commit_sha", return_value=None),
        ]

    def test_splits_build_then_test_with_no_rebuild(self):
        sim = mock.MagicMock()
        sim.build.return_value = None                 # build ok
        captured = {}

        def fake_func(**kw):
            captured["build"] = kw.get("build")
            return None                                # PASS

        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(sim, {"smoke": fake_func}):
                es.enter_context(p)
            outcome = executor._run_test_direct("mm1k", "smoke", recorder=rec)
        self.assertEqual([s["name"] for s in rec.stages],
                         [Stage.PROJECT_BUILD, Stage.TEST_RUN])
        self.assertEqual(outcome["result_code"], "PASS")
        sim.build.assert_called_once()
        self.assertIs(captured["build"], False)        # test stage doesn't rebuild

    def test_build_failure_skips_test(self):
        sim = mock.MagicMock()
        bad = mock.MagicMock()
        bad.is_all_results_expected.return_value = False   # compile failed
        sim.build.return_value = bad

        def fake_func(**kw):
            raise AssertionError("test must not run after a failed build")

        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(sim, {"smoke": fake_func}):
                es.enter_context(p)
            outcome = executor._run_test_direct("mm1k", "smoke", recorder=rec)
        self.assertEqual([(s["name"], s["status"]) for s in rec.stages],
                         [(Stage.PROJECT_BUILD, FAILED), (Stage.TEST_RUN, SKIPPED)])
        self.assertEqual(outcome["result_code"], "FAIL")

    def test_kind_build_has_no_test_stage(self):
        sim = mock.MagicMock()
        sim.build.return_value = None
        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(sim, {}):
                es.enter_context(p)
            outcome = executor._run_test_direct("mm1k", "build", recorder=rec)
        self.assertEqual([s["name"] for s in rec.stages], [Stage.PROJECT_BUILD])
        self.assertEqual(outcome["result_code"], "PASS")

    def test_skip_build_runs_test_only(self):
        sim = mock.MagicMock()
        captured = {}

        def fake_func(**kw):
            captured["build"] = kw.get("build")
            return None

        rec = StageRecorder()
        with contextlib.ExitStack() as es:
            for p in self._patches(sim, {"smoke": fake_func}):
                es.enter_context(p)
            outcome = executor._run_test_direct(
                "mm1k", "smoke", recorder=rec, skip_build=True)
        self.assertEqual([s["name"] for s in rec.stages], [Stage.TEST_RUN])  # no build
        sim.build.assert_not_called()
        self.assertIs(captured["build"], False)
        self.assertEqual(outcome["result_code"], "PASS")


class RunOutputStoreTests(unittest.TestCase):
    def test_events_build_stages_and_lines(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(1, [
            _ev("stage_begin", stage="build", ordinal=0, command="c0"),
            _ev("output", stage="build", stream="out", text="a"),
            _ev("stage_end", stage="build", exit=0, status="passed"),
            _ev("stage_begin", stage="test", ordinal=1, command="c1"),
            _ev("output", stage="test", stream="err", text="b"),
        ])
        stages, lines, last = s.snapshot(1, 0)
        self.assertEqual([st["name"] for st in stages], ["build", "test"])
        self.assertEqual(stages[0]["status"], "passed")
        self.assertEqual(stages[0]["exit"], 0)
        self.assertEqual(stages[1]["status"], "running")
        self.assertEqual([(l["ordinal"], l["stream"], l["text"]) for l in lines],
                         [(0, "out", "a"), (1, "err", "b")])
        self.assertEqual(last, 2)

    def test_snapshot_incremental_keeps_full_stages(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(1, [_ev("stage_begin", stage="build", ordinal=0),
                     _ev("output", stage="build", stream="out", text="a")])
        _, _, last = s.snapshot(1, 0)
        s.append(1, [_ev("output", stage="build", stream="out", text="b")])
        stages, lines, last2 = s.snapshot(1, last)
        self.assertEqual(len(stages), 1)                       # full stage list
        self.assertEqual([l["text"] for l in lines], ["b"])    # only new line
        self.assertEqual(last2, 2)

    def test_has_and_drop(self):
        s = RunOutputStore(ring=100, max_runs=10)
        s.append(5, [_ev("output", stage=None, stream="out", text="x")])
        self.assertTrue(s.has(5))
        s.drop(5)
        self.assertFalse(s.has(5))

    def test_lru_evicts_oldest_run(self):
        s = RunOutputStore(ring=100, max_runs=2)
        s.append(1, [_ev("output", stage=None, stream="out", text="a")])
        s.append(2, [_ev("output", stage=None, stream="out", text="b")])
        s.append(1, [_ev("output", stage=None, stream="out", text="a2")])  # touch 1
        s.append(3, [_ev("output", stage=None, stream="out", text="c")])   # evict 2
        self.assertTrue(s.has(1))
        self.assertFalse(s.has(2))
        self.assertTrue(s.has(3))


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
    def test_flush_batches_events(self):
        sess = _FakeSession()
        st = _RunOutputStreamer(sess, "http://c", 42)
        st.append(_ev("stage_begin", stage="test", ordinal=0))
        st.append(_ev("output", stage="test", stream="out", text="hi"))
        st._flush()
        self.assertEqual(sess.posts[0]["url"], "http://c/api/runs/42/output-append")
        self.assertEqual(len(sess.posts[0]["json"]["events"]), 2)
        st._flush()
        self.assertEqual(len(sess.posts), 1)                  # nothing new

    def test_flush_failure_swallowed(self):
        import requests
        sess = _FakeSession(raise_exc=requests.RequestException("down"))
        st = _RunOutputStreamer(sess, "http://c", 1)
        st.append(_ev("output", stage="t", stream="out", text="x"))
        st._flush()                                           # must not raise

    def test_stop_does_final_flush(self):
        sess = _FakeSession()
        st = _RunOutputStreamer(sess, "http://c", 7)
        st.start()
        st.append(_ev("output", stage="t", stream="out", text="late"))
        st.stop()
        texts = [e.get("text") for p in sess.posts for e in p["json"]["events"]]
        self.assertIn("late", texts)


class RunOutputRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8201, username="rout-sub", role="submitter", enabled=True))
            s.add(Worker(id=8210, name="rout-owner", token="tok-rout-own", status="online"))
            s.add(Worker(id=8211, name="rout-other", token="tok-rout-other", status="online"))
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
                return s.execute(select(User).where(User.id == 8201)).scalar_one_or_none()
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
        ev = [_ev("output", stage="test.run", stream="out", text="x")]
        r = self.client.post(
            f"/api/runs/{self.run_id}/output-append",
            headers={"Authorization": "Bearer tok-rout-other"}, json={"events": ev})
        self.assertEqual(r.json()["status"], "dropped")
        self.assertFalse(STORE.has(self.run_id))
        r = self.client.post(
            f"/api/runs/{self.run_id}/output-append",
            headers={"Authorization": "Bearer tok-rout-own"}, json={"events": ev})
        self.assertEqual(r.json()["status"], "ok")
        self.assertTrue(STORE.has(self.run_id))

    def test_append_unknown_run_404(self):
        r = self.client.post(
            "/api/runs/999999/output-append",
            headers={"Authorization": "Bearer tok-rout-own"},
            json={"events": [_ev("output", stage="t", stream="out", text="x")]})
        self.assertEqual(r.status_code, 404)

    def test_tail_returns_stages_lines_and_done(self):
        STORE.drop(self.run_id)
        STORE.append(self.run_id, [
            _ev("stage_begin", stage="test.run", ordinal=0, command="run it"),
            _ev("output", stage="test.run", stream="err", text="<b>boom</b>"),
        ])
        data = self.client.get(f"/test-runs/{self.run_id}/output/tail").json()
        self.assertTrue(data["available"])
        self.assertFalse(data["done"])
        self.assertEqual(data["stages"][0]["name"], "test.run")
        self.assertEqual(data["stages"][0]["status"], "running")
        line = data["lines"][0]
        self.assertEqual(line["ordinal"], 0)
        self.assertEqual(line["stream"], "err")
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", line["html"])
        # re-poll with cursor → no new lines, stages still present
        data2 = self.client.get(
            f"/test-runs/{self.run_id}/output/tail?cursor={data['cursor']}").json()
        self.assertEqual(data2["lines"], [])
        self.assertEqual(len(data2["stages"]), 1)
        # flip lifecycle terminal → done
        s = SessionLocal()
        try:
            run = s.execute(
                select(webapp.TestRun).where(webapp.TestRun.id == self.run_id)).scalar_one()
            run.lifecycle = TestRunLifecycle.finished
            s.commit()
        finally:
            s.close()
        self.assertTrue(
            self.client.get(f"/test-runs/{self.run_id}/output/tail").json()["done"])


if __name__ == "__main__":
    unittest.main()
