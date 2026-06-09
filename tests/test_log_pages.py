"""Tests for the web UI Logs pages (plan/pending/log-pages.md).

Covers:
  * journal.worker_unit_name — verbatim mapping + charset guard
  * journal.read_unit — initial vs incremental (cursor) command shape,
    byte-array MESSAGE decoding, empty result, error/missing handling
  * the web routes — hub + viewers render, tail endpoints shape the JSON
    (available / reason / escaped html), admin-gating, unknown worker 404

Run with: python -m unittest tests.test_log_pages   (no pytest needed)
"""

import json
import os
import tempfile
import types
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_logs_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"
os.environ.setdefault("OPP_CI_SESSION_SECRET", "x" * 40)

from opp_ci import config as _cfg                                # noqa: E402
_cfg.SESSION_SECRET = _cfg.SESSION_SECRET or "x" * 40

from fastapi.testclient import TestClient                       # noqa: E402
from sqlalchemy import select                                   # noqa: E402

from opp_ci import journal                                      # noqa: E402
from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, User, Worker                 # noqa: E402
from opp_ci.web import app as webapp                            # noqa: E402


def _fake_proc(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _entry(message, cursor="c1", priority="6", ts="1700000000000000"):
    return json.dumps({
        "MESSAGE": message, "PRIORITY": priority,
        "__CURSOR": cursor, "__REALTIME_TIMESTAMP": ts,
    })


class JournalUnitNameTests(unittest.TestCase):
    def test_verbatim(self):
        self.assertEqual(journal.worker_unit_name("local"),
                         "opp_ci-worker@local.service")
        self.assertEqual(journal.worker_unit_name("builder-1"),
                         "opp_ci-worker@builder-1.service")

    def test_rejects_unsafe(self):
        for bad in ["", "a/b", "has space", "x\ny", "a;b"]:
            with self.assertRaises(journal.JournalUnavailable):
                journal.worker_unit_name(bad)


class ReadUnitTests(unittest.TestCase):
    def _run_with(self, proc, **kw):
        with mock.patch("opp_ci.journal.shutil.which", return_value="/usr/bin/journalctl"), \
             mock.patch("opp_ci.journal.subprocess.run", return_value=proc) as run:
            result = journal.read_unit("opp_ci-serve.service", **kw)
        return result, run

    def test_initial_load_uses_lines(self):
        (entries, cursor), run = self._run_with(_fake_proc(stdout=_entry("hello") + "\n"))
        self.assertEqual(cursor, "c1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"], "hello")
        self.assertEqual(entries[0]["priority"], 6)
        self.assertIsNotNone(entries[0]["ts"])
        argv = run.call_args[0][0]
        self.assertIn("--lines", argv)
        self.assertNotIn("--after-cursor", argv)

    def test_incremental_uses_after_cursor(self):
        (entries, cursor), run = self._run_with(
            _fake_proc(stdout=_entry("more", cursor="c2") + "\n"), cursor="c1")
        self.assertEqual(cursor, "c2")
        argv = run.call_args[0][0]
        self.assertIn("--after-cursor", argv)
        self.assertIn("c1", argv)
        self.assertNotIn("--lines", argv)

    def test_empty_incremental_keeps_cursor(self):
        (entries, cursor), _ = self._run_with(_fake_proc(stdout=""), cursor="c9")
        self.assertEqual(entries, [])
        self.assertEqual(cursor, "c9")  # poll position never goes backwards

    def test_byte_array_message_decoded(self):
        (entries, _), _ = self._run_with(_fake_proc(stdout=_entry([104, 105]) + "\n"))
        self.assertEqual(entries[0]["message"], "hi")

    def test_nonzero_exit_raises_with_reason(self):
        with self.assertRaises(journal.JournalUnavailable) as cm:
            self._run_with(_fake_proc(returncode=1, stderr="Permission denied\n"))
        self.assertIn("Permission denied", cm.exception.reason)

    def test_missing_journalctl_raises(self):
        with mock.patch("opp_ci.journal.shutil.which", return_value=None):
            with self.assertRaises(journal.JournalUnavailable):
                journal.read_unit("opp_ci-serve.service")


class LogRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use high, distinctive ids: under `unittest discover` all test
        # modules share one engine (bound at first import), so the seeded
        # rows must not collide with another module's id=1 admin.
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8001, username="logs-admin", role="admin", enabled=True))
            s.add(User(id=8002, username="logs-ro", role="readonly", enabled=True))
            s.add(User(id=8003, username="logs-sub", role="submitter", enabled=True))
            s.add(Worker(id=8005, name="local", token="tok-logs-local"))
            s.commit()
        finally:
            s.close()

        cls.role = "admin"
        cls._uid_for = {"admin": 8001, "readonly": 8002, "submitter": 8003}

        def _load(_uid):
            s = SessionLocal()
            try:
                uid = cls._uid_for[cls.role]
                return s.execute(select(User).where(User.id == uid)).scalar_one_or_none()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()

    def setUp(self):
        type(self).role = "admin"

    # ── pages ──────────────────────────────────────────────────────────
    def test_hub_lists_serve_and_worker(self):
        r = self.client.get("/logs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Serve", r.text)
        self.assertIn("local", r.text)

    def test_serve_and_worker_viewers_render(self):
        self.assertEqual(self.client.get("/logs/serve").status_code, 200)
        self.assertEqual(self.client.get("/logs/worker/8005").status_code, 200)

    def test_unknown_worker_404(self):
        self.assertEqual(self.client.get("/logs/worker/999").status_code, 404)
        self.assertEqual(self.client.get("/logs/worker/999/tail").status_code, 404)

    # ── tail JSON ──────────────────────────────────────────────────────
    def test_tail_available_escapes_html(self):
        canned = ([{"ts": None, "priority": 6,
                    "message": "<b>hi</b>", "cursor": "cZ"}], "cZ")
        with mock.patch("opp_ci.journal.read_unit", return_value=canned):
            r = self.client.get("/logs/serve/tail")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["available"])
        self.assertEqual(data["cursor"], "cZ")
        html = data["entries"][0]["html"]
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", html)
        self.assertNotIn("<b>hi</b>", html)

    def test_tail_unavailable_reports_reason(self):
        with mock.patch("opp_ci.journal.read_unit",
                        side_effect=journal.JournalUnavailable("no access")):
            r = self.client.get("/logs/serve/tail")
        data = r.json()
        self.assertFalse(data["available"])
        self.assertEqual(data["reason"], "no access")
        self.assertEqual(data["entries"], [])

    def test_worker_tail_resolves_unit(self):
        captured = {}

        def _fake_read(unit, **kw):
            captured["unit"] = unit
            return [], None

        with mock.patch("opp_ci.journal.read_unit", side_effect=_fake_read):
            r = self.client.get("/logs/worker/8005/tail")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["unit"], "opp_ci-worker@local.service")

    # ── auth ───────────────────────────────────────────────────────────
    def test_readonly_forbidden(self):
        type(self).role = "readonly"
        self.assertEqual(self.client.get("/logs").status_code, 403)
        self.assertEqual(self.client.get("/logs/serve").status_code, 403)
        self.assertEqual(self.client.get("/logs/serve/tail").status_code, 403)

    def test_submitter_allowed(self):
        type(self).role = "submitter"
        self.assertEqual(self.client.get("/logs").status_code, 200)
        self.assertEqual(self.client.get("/logs/serve").status_code, 200)
        with mock.patch("opp_ci.journal.read_unit", return_value=([], None)):
            self.assertEqual(self.client.get("/logs/serve/tail").status_code, 200)


if __name__ == "__main__":
    unittest.main()
