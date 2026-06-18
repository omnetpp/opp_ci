"""Web test-creation form: on an under-specified submission the form must
re-render in place (state preserved) with a specific message naming what to
fill in — not redirect to a blank form.

Run with: python -m unittest tests.test_test_form
"""

import os
import re
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_form_")
os.close(_DB_FD)
os.environ.setdefault("OPP_CI_DATABASE_URL", f"sqlite:///{_DB_PATH}")
os.environ.setdefault("OPP_CI_REMOTE", "0")
os.environ.setdefault("OPP_CI_SESSION_SECRET", "test-secret-for-form-tests")

from fastapi.testclient import TestClient                       # noqa: E402

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, Project, Test, User          # noqa: E402
from opp_ci.web import app as webapp                            # noqa: E402


class TestFormStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8101, username="form-sub", role="submitter", enabled=True))
            if not s.query(Project).filter_by(name="inet").first():
                s.add(Project(name="inet"))
            s.commit()
        finally:
            s.close()

        def _load(_uid):
            s = SessionLocal()
            try:
                return s.query(User).filter_by(id=8101).first()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()

    def _csrf(self):
        """GET the form (sets the session cookie) and return its CSRF token."""
        r = self.client.get("/tests/new")
        self.assertEqual(r.status_code, 200, r.text)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertIsNotNone(m, "csrf token not found in form")
        return m.group(1)

    def test_under_specified_run_reports_and_preserves_state(self):
        token = self._csrf()
        # action=run eager-resolves against the fleet; with no workers it can't,
        # so it reports (in place, state preserved). (action=save would instead
        # persist a recipe — see test_under_specified_save_creates_recipe.)
        r = self.client.post("/tests/new", data={
            "csrf_token": token,
            "action": "run",
            "project": "inet", "kind": "fingerprint",
            "os": "Linux", "distro": "Ubuntu",
            # deliberately omitted: distro_version, arch, mode, compiler,
            # compiler_version
        }, follow_redirects=False)

        # Re-rendered in place (not a redirect to a blank form).
        self.assertEqual(r.status_code, 400, r.text)
        body = r.text

        # Specific: the flash message names the missing dimensions, using the
        # form's own field labels. Scope the assertion to the "Missing/empty:"
        # list so it can't pass on the page's <label> text.
        self.assertIn("under-specifies", body)
        # With no workers, resolution can't supply the loose axes — the message
        # names the fleet as the cause, not the user.
        self.assertIn("against the fleet", body)
        m = re.search(r"Missing/empty:\s*([^<]+)", body)
        self.assertIsNotNone(m, "missing-field list not found in flash")
        listed = m.group(1)
        for label in ("Architecture", "Build Mode", "Compiler Version",
                      "Distro Version"):
            self.assertIn(label, listed)

        # State preserved: what the user typed is echoed back.
        self.assertRegex(body, r'name="distro"[^>]*value="Ubuntu"')
        self.assertRegex(body, r'value="inet" selected')
        # kind select kept the chosen option
        self.assertRegex(body, r'value="fingerprint" selected')

    def test_fully_specified_submission_succeeds(self):
        token = self._csrf()
        r = self.client.post("/tests/new", data={
            "csrf_token": token,
            "action": "save",
            "project": "inet", "kind": "smoke", "mode": "release",
            "os": "Linux", "distro": "Ubuntu", "distro_version": "24.04",
            "arch": "amd64", "compiler": "gcc", "compiler_version": "14",
        }, follow_redirects=False)
        # On success the handler redirects to the test detail page.
        self.assertIn(r.status_code, (302, 303), r.text)
        self.assertIn("/tests/", r.headers.get("location", ""))

    def test_form_exposes_git_ref_and_omnetpp_git_fields(self):
        # The UI must let you pin a source ref (inet master) and a git-ref
        # OMNeT++ dependency (git@omnetpp-6.x) — the latter is a free-text
        # input with suggestions, not a fixed release dropdown.
        r = self.client.get("/tests/new")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('name="git_ref"', html)                     # source ref
        self.assertIn('name="omnetpp_version" id="omnetpp_version" list=', html)
        self.assertNotIn('<select name="omnetpp_version"', html)   # no longer a dropdown
        self.assertIn("git@omnetpp-6.x", html)                     # documented in UI

    def test_under_specified_save_creates_recipe(self):
        token = self._csrf()
        # Underspecified + Save → persisted as a recipe (not an error).
        r = self.client.post("/tests/new", data={
            "csrf_token": token,
            "action": "save",
            "project": "inet", "kind": "smoke",
            "os": "Linux", "distro": "Ubuntu",
            # no distro_version/arch/mode/compiler → a recipe
        }, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text)
        loc = r.headers.get("location", "")
        self.assertRegex(loc, r"/tests/\d+")
        test_id = int(loc.rstrip("/").split("/")[-1])
        s = SessionLocal()
        try:
            t = s.get(Test, test_id)
            self.assertIsNotNone(t)
            self.assertFalse(t.is_resolved)   # it's a recipe
            self.assertIsNone(t.compiler)     # loose axis, unvalidated
        finally:
            s.close()


class MatrixFormGitDepTests(unittest.TestCase):
    """The matrix form's OMNeT++ Versions field accepts a git ref, parsed into
    a git-ref dep object that resolution later pins to a commit."""

    def _cfg(self, omnetpp_versions):
        return webapp._build_matrix_config_from_form(
            project="inet", kinds="smoke", modes="", versions="",
            omnetpp_versions=omnetpp_versions, refs="", os="", os_version="",
            distro="", distro_version="", flavor="", flavor_version="",
            arch="", compiler="", compiler_version="", isolation="",
            toolchain="", ref_range_base="", ref_range_head="")

    def test_release_versions_unchanged(self):
        self.assertEqual(self._cfg("6.4.0, 6.3.0")["deps"],
                         {"omnetpp": ["6.4.0", "6.3.0"]})

    def test_git_ref_parsed_to_object(self):
        self.assertEqual(self._cfg("git@omnetpp-6.x")["deps"],
                         {"omnetpp": [{"git": "omnetpp-6.x"}]})

    def test_mixed_release_and_git(self):
        self.assertEqual(self._cfg("6.4.0, git@omnetpp-6.x")["deps"],
                         {"omnetpp": ["6.4.0", {"git": "omnetpp-6.x"}]})


if __name__ == "__main__":
    unittest.main()
