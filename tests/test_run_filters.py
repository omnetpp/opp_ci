"""Tests for worker run-filters: opting out of Tests by coordinate axis.

Run-filters are a *willingness* gate orthogonal to capability `tags`: a worker
may decline a Test it is capable of running. Layers covered:

1. persistence.{validate_run_filters, worker_accepts_test, worker_can_serve,
   format_run_filters} — pure predicates / validation.
2. cli.{_parse_run_filters_opts, _resolve_run_filters} — option parsing/merge.
3. persistence.expire_unserviceable_queued_runs — an opted-out run is expired
   with a filter-specific cause; a willing worker keeps it queued.
4. REST + poll — register/patch round-trip and dispatch respecting willingness.

Run with: python -m unittest tests.test_run_filters   (no pytest needed)

The DB url must be set before importing opp_ci.db, so this module pokes
os.environ at import time.
"""

import datetime
import os
import tempfile
import unittest

import click

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_runfilters_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"

from fastapi import FastAPI                                       # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from opp_ci import cli                                           # noqa: E402
from opp_ci.db.connection import get_engine, SessionLocal        # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    ApiToken, Base, TestResultCode, TestRunLifecycle, Worker,
)
from opp_ci.persistence import (                                 # noqa: E402
    create_matrix_from_axes, create_matrix_run, create_test_run,
    create_test_verdict, expire_unserviceable_queued_runs, format_run_filters,
    get_or_create_test, validate_run_filters, worker_accepts_test,
    worker_can_serve,
)
from opp_ci.web.api import router                                # noqa: E402

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


class _FakeTest:
    """A Test-shaped object with every coordinate attribute, for the pure
    predicate tests (no DB row needed)."""
    def __init__(self, **over):
        for k, v in _coord(**over).items():
            setattr(self, k, v)


class ValidateRunFiltersTests(unittest.TestCase):
    def test_valid_canonicalized(self):
        out = validate_run_filters({"isolation": {"deny": ["podman", "podman"]},
                                    "toolchain": {"allow": ["nix", "none"]}})
        self.assertEqual(out, {"isolation": {"deny": ["podman"]},
                               "toolchain": {"allow": ["nix", "none"]}})

    def test_empty_is_empty(self):
        self.assertEqual(validate_run_filters(None), {})
        self.assertEqual(validate_run_filters({}), {})

    def test_reject_both_allow_and_deny(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {"allow": ["none"], "deny": ["podman"]}})

    def test_reject_neither(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {}})

    def test_reject_unknown_axis(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"frobnicate": {"deny": ["x"]}})

    def test_reject_unknown_known_axis_value(self):
        # isolation/toolchain values are a closed set — a typo is rejected.
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {"deny": ["podmann"]}})
        with self.assertRaises(ValueError):
            validate_run_filters({"toolchain": {"allow": ["nyx"]}})

    def test_open_axis_value_accepted(self):
        # compiler has open-ended values — anything goes.
        out = validate_run_filters({"compiler": {"deny": ["gcc-7"]}})
        self.assertEqual(out, {"compiler": {"deny": ["gcc-7"]}})

    def test_reject_empty_value_list(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {"deny": []}})

    def test_reject_non_string_value(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {"deny": [7]}})

    def test_reject_unexpected_key(self):
        with self.assertRaises(ValueError):
            validate_run_filters({"isolation": {"deny": ["podman"], "maybe": ["x"]}})


class WorkerAcceptsTestTests(unittest.TestCase):
    def test_empty_accepts_all(self):
        self.assertTrue(worker_accepts_test({}, _FakeTest(isolation="podman")))

    def test_deny_blocks_named_value(self):
        f = {"isolation": {"deny": ["podman"]}}
        self.assertFalse(worker_accepts_test(f, _FakeTest(isolation="podman")))
        self.assertTrue(worker_accepts_test(f, _FakeTest(isolation="none")))

    def test_deny_default_none_value(self):
        # isolation=None normalizes to "none".
        f = {"isolation": {"deny": ["none"]}}
        self.assertFalse(worker_accepts_test(f, _FakeTest(isolation=None)))

    def test_allow_only_named_value(self):
        f = {"isolation": {"allow": ["podman"]}}
        self.assertTrue(worker_accepts_test(f, _FakeTest(isolation="podman")))
        # allow-list excludes the default 'none' too
        self.assertFalse(worker_accepts_test(f, _FakeTest(isolation="none")))

    def test_toolchain_axis(self):
        f = {"toolchain": {"deny": ["nix"]}}
        self.assertFalse(worker_accepts_test(f, _FakeTest(toolchain="nix")))
        self.assertTrue(worker_accepts_test(f, _FakeTest(toolchain="none")))

    def test_multiple_axes_all_must_pass(self):
        f = {"isolation": {"deny": ["podman"]}, "toolchain": {"deny": ["nix"]}}
        self.assertTrue(worker_accepts_test(f, _FakeTest()))  # none/none
        self.assertFalse(worker_accepts_test(f, _FakeTest(toolchain="nix")))

    def test_general_axis(self):
        f = {"compiler": {"deny": ["gcc"]}}
        self.assertFalse(worker_accepts_test(f, _FakeTest(compiler="gcc")))
        self.assertTrue(worker_accepts_test(f, _FakeTest(compiler="clang")))


class WorkerCanServeTests(unittest.TestCase):
    def test_capable_and_willing(self):
        w = Worker(name="w", tags=["podman"], run_filters={})
        self.assertTrue(worker_can_serve(w, _FakeTest(isolation="podman")))

    def test_capable_but_unwilling(self):
        w = Worker(name="w", tags=["podman"], run_filters={"isolation": {"deny": ["podman"]}})
        self.assertFalse(worker_can_serve(w, _FakeTest(isolation="podman")))

    def test_incapable(self):
        w = Worker(name="w", tags=[], run_filters={})
        self.assertFalse(worker_can_serve(w, _FakeTest(isolation="podman")))

    def test_willing_for_other_axis_value(self):
        # Worker denies podman but the test is bare-metal: still serves it
        # (it is capable — the Linux test needs only the os:linux tag).
        w = Worker(name="w", tags=["os:linux"],
                   run_filters={"isolation": {"deny": ["podman"]}})
        self.assertTrue(worker_can_serve(w, _FakeTest(isolation="none")))


class FormatRunFiltersTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_run_filters(None), "-")
        self.assertEqual(format_run_filters({}), "-")

    def test_rendering(self):
        s = format_run_filters({"isolation": {"deny": ["podman"]},
                                "toolchain": {"allow": ["nix", "none"]}})
        self.assertEqual(s, "isolation:deny[podman] toolchain:allow[nix,none]")


class CliParsingTests(unittest.TestCase):
    def _parse(self, **kw):
        base = dict(accept_isolation=None, deny_isolation=None,
                    accept_toolchain=None, deny_toolchain=None, run_filter=())
        base.update(kw)
        return cli._parse_run_filters_opts(**base)

    def test_shortcuts(self):
        self.assertEqual(self._parse(deny_isolation="podman"),
                         {"isolation": {"deny": ["podman"]}})
        self.assertEqual(self._parse(accept_isolation="none,podman"),
                         {"isolation": {"allow": ["none", "podman"]}})

    def test_general_run_filter(self):
        self.assertEqual(self._parse(run_filter=("compiler=deny:gcc-7,gcc-8",)),
                         {"compiler": {"deny": ["gcc-7", "gcc-8"]}})

    def test_conflict_allow_and_deny(self):
        with self.assertRaises(click.UsageError):
            self._parse(accept_isolation="none", deny_isolation="podman")

    def test_malformed_run_filter(self):
        with self.assertRaises(click.UsageError):
            self._parse(run_filter=("isolation",))          # no '='
        with self.assertRaises(click.UsageError):
            self._parse(run_filter=("isolation=podman",))   # no mode ':'
        with self.assertRaises(click.UsageError):
            self._parse(run_filter=("isolation=maybe:x",))  # bad mode

    def test_empty_parse(self):
        self.assertEqual(self._parse(), {})

    def test_resolve_merge_and_clear(self):
        current = {"isolation": {"deny": ["podman"]}, "compiler": {"deny": ["gcc-7"]}}
        # set toolchain, keep the rest
        out = cli._resolve_run_filters(current, {"toolchain": {"deny": ["nix"]}},
                                       False, ())
        self.assertEqual(out["toolchain"], {"deny": ["nix"]})
        self.assertIn("isolation", out)
        self.assertIn("compiler", out)
        # clear one axis
        out = cli._resolve_run_filters(current, {}, False, ("isolation",))
        self.assertNotIn("isolation", out)
        self.assertIn("compiler", out)
        # clear all
        self.assertEqual(cli._resolve_run_filters(current, {}, True, ()), {})
        # nothing requested → None (leave untouched)
        self.assertIsNone(cli._resolve_run_filters(current, {}, False, ()))


class SweepTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _worker(self, *, tags, run_filters=None, enabled=True, name="w1"):
        w = Worker(name=name, token="tok-" + name, tags=tags,
                   run_filters=run_filters or {}, status="online",
                   enabled=enabled, last_heartbeat=_NOW)
        self.s.add(w)
        self.s.flush()
        return w

    def _queued_run(self, *, coord_over):
        test = get_or_create_test(self.s, _coord(**coord_over))
        mtx = create_matrix_from_axes(self.s, project="mm1k", config={})
        mr = create_matrix_run(self.s, matrix_id=mtx.id)
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=mr.id)
        create_test_verdict(self.s, matrix_run_id=mr.id, test_id=test.id,
                            test_run_id=run.id)
        run.created_at = _OLD
        self.s.flush()
        return run

    def test_opted_out_run_expired_with_filter_cause(self):
        # The only capable worker (has podman tag) opts out of podman.
        self._worker(tags=["podman"], run_filters={"isolation": {"deny": ["podman"]}})
        run = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)
        self.s.commit()

        self.assertEqual(expired, 1)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.timed_out)
        self.assertEqual(run.result_code, TestResultCode.ERROR)
        self.assertTrue(run.details.get("unserviceable"))
        self.assertTrue(run.details.get("declined_by_filter"))
        self.assertIn("opts out", run.stderr)

    def test_missing_tags_cause_distinct_from_optout(self):
        # No worker even has the podman tag → not a filter decline.
        self._worker(tags=["os:linux"])
        run = self._queued_run(coord_over={"isolation": "podman"})

        expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)
        self.s.commit()

        self.s.refresh(run)
        self.assertTrue(run.details.get("unserviceable"))
        self.assertFalse(run.details.get("declined_by_filter"))
        self.assertIn("required tags", run.stderr)

    def test_willing_worker_keeps_run_queued(self):
        self._worker(tags=["podman"], run_filters={})  # capable + willing
        run = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)

        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)

    def test_one_willing_among_several_keeps_queued(self):
        self._worker(tags=["podman"], run_filters={"isolation": {"deny": ["podman"]}},
                     name="opted-out")
        self._worker(tags=["podman"], run_filters={}, name="willing")
        run = self._queued_run(coord_over={"isolation": "podman"})

        expired = expire_unserviceable_queued_runs(
            self.s, _NOW, self.s.query(Worker).all(), 300)

        self.assertEqual(expired, 0)
        self.s.refresh(run)
        self.assertEqual(run.lifecycle, TestRunLifecycle.queued)


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mint_token(role):
    session = SessionLocal()
    try:
        tok = ApiToken(name=f"{role}-tok", role=role)
        session.add(tok)
        session.commit()
        return tok.token
    finally:
        session.close()


class RestRunFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        cls.client = TestClient(_make_app())
        cls.admin = _mint_token("admin")

    def _h(self):
        return {"Authorization": f"Bearer {self.admin}"}

    def test_register_with_run_filters_roundtrip(self):
        r = self.client.post("/api/workers/register",
                             json={"name": "rf-1", "tags": ["podman"],
                                   "run_filters": {"isolation": {"deny": ["podman"]}}},
                             headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        wid = r.json()["id"]
        listing = self.client.get("/api/workers", headers=self._h()).json()
        match = [w for w in listing if w["id"] == wid][0]
        self.assertEqual(match["run_filters"], {"isolation": {"deny": ["podman"]}})

    def test_register_invalid_run_filters_400(self):
        r = self.client.post("/api/workers/register",
                             json={"name": "rf-bad",
                                   "run_filters": {"isolation": {"deny": ["podmann"]}}},
                             headers=self._h())
        self.assertEqual(r.status_code, 400, r.text)

    def test_patch_sets_and_clears(self):
        wid = self.client.post("/api/workers/register",
                              json={"name": "rf-2", "tags": ["nix"]},
                              headers=self._h()).json()["id"]
        # set
        r = self.client.patch(f"/api/workers/{wid}",
                             json={"run_filters": {"toolchain": {"deny": ["nix"]}}},
                             headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["run_filters"], {"toolchain": {"deny": ["nix"]}})
        # clear
        r = self.client.patch(f"/api/workers/{wid}", json={"run_filters": {}},
                             headers=self._h())
        self.assertEqual(r.json()["run_filters"], {})

    def test_me_includes_run_filters(self):
        r = self.client.post("/api/workers/register",
                            json={"name": "rf-me", "tags": ["podman"],
                                  "run_filters": {"isolation": {"allow": ["podman"]}}},
                            headers=self._h())
        wid = r.json()["id"]
        session = SessionLocal()
        try:
            token = session.get(Worker, wid).token
        finally:
            session.close()
        me = self.client.get("/api/workers/me",
                             headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(me["run_filters"], {"isolation": {"allow": ["podman"]}})


class PollWillingnessTests(unittest.TestCase):
    """The poll dispatcher must not hand an opted-out worker a job it declines."""

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        cls.client = TestClient(_make_app())
        cls.admin = _mint_token("admin")

    def _admin_h(self):
        return {"Authorization": f"Bearer {self.admin}"}

    def _register(self, name, run_filters):
        r = self.client.post("/api/workers/register",
                             json={"name": name, "tags": ["podman"],
                                   "run_filters": run_filters},
                             headers=self._admin_h())
        wid = r.json()["id"]
        session = SessionLocal()
        try:
            w = session.get(Worker, wid)
            w.status = "online"
            w.last_heartbeat = datetime.datetime.utcnow()
            token = w.token
            session.commit()
        finally:
            session.close()
        return wid, token

    def _enqueue_podman_run(self):
        session = SessionLocal()
        try:
            test = get_or_create_test(session, _coord(isolation="podman"))
            run = create_test_run(session, test_id=test.id)
            session.commit()
            return run.id
        finally:
            session.close()

    def test_opted_out_worker_gets_no_job_then_does_after_clear(self):
        wid, token = self._register("poller", {"isolation": {"deny": ["podman"]}})
        self._enqueue_podman_run()
        wh = {"Authorization": f"Bearer {token}"}

        # Declines the podman job → no job handed out.
        resp = self.client.post("/api/workers/poll", headers=wh).json()
        self.assertIsNone(resp["job"])

        # Clear the filter; now it claims the job.
        self.client.patch(f"/api/workers/{wid}", json={"run_filters": {}},
                         headers=self._admin_h())
        resp = self.client.post("/api/workers/poll", headers=wh).json()
        self.assertIsNotNone(resp["job"])
        self.assertEqual(resp["job"]["isolation"], "podman")


if __name__ == "__main__":
    unittest.main()
