"""A TestMatrix run resolves loose axes against the fleet like a single Test,
and an axis the fleet can't resolve surfaces as a clean error on the matrix
page — not a 500.

Run with: python -m unittest tests.test_matrix_run_resolve
"""

import os
import re
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_mrun_")
os.close(_DB_FD)
os.environ.setdefault("OPP_CI_DATABASE_URL", f"sqlite:///{_DB_PATH}")
os.environ.setdefault("OPP_CI_REMOTE", "0")
os.environ.setdefault("OPP_CI_SESSION_SECRET", "test-secret-for-matrix-run-tests")

from fastapi.testclient import TestClient                       # noqa: E402

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, Project, User, Worker         # noqa: E402
from opp_ci.persistence import create_matrix_from_axes          # noqa: E402
from opp_ci.web import app as webapp                             # noqa: E402


class MatrixRunResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(User(id=8201, username="mrun-sub", role="submitter",
                       enabled=True))
            if not s.query(Project).filter_by(name="inet").first():
                s.add(Project(name="inet"))
            # The only platform the fleet advertises is ubuntu-24.04.
            s.add(Worker(name="w-mrun", tags=["compiler:gcc-14", "arch:amd64",
                                              "distro:ubuntu-24.04"]))
            s.commit()
        finally:
            s.close()

        def _load(_uid):
            s = SessionLocal()
            try:
                return s.query(User).filter_by(id=8201).first()
            finally:
                s.close()

        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()

    def _csrf(self):
        r = self.client.get("/tests/new")
        self.assertEqual(r.status_code, 200, r.text)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        self.assertIsNotNone(m, "csrf token not found in form")
        return m.group(1)

    def _make_matrix(self, distro):
        s = SessionLocal()
        try:
            m = create_matrix_from_axes(
                s, project="inet",
                config={"kinds": ["build"], "compiler": ["gcc-14"],
                        "arch": ["amd64"], "os": ["Linux"], "distro": [distro]})
            s.commit()
            return m.id
        finally:
            s.close()

    def _run(self, matrix_id):
        return self.client.post(
            f"/test-matrices/{matrix_id}/run",
            data={"csrf_token": self._csrf()},
            follow_redirects=False)

    def test_unresolvable_distro_version_redirects_with_error_not_500(self):
        # distro=fedora has no version in the fleet → the loose distro_version
        # can't resolve. The handler must surface that as a clean redirect to
        # the matrix page with ?error=, not crash with a 500.
        mid = self._make_matrix("fedora")
        r = self._run(mid)
        self.assertEqual(r.status_code, 303, r.text)
        loc = r.headers["location"]
        self.assertIn(f"/test-matrices/{mid}", loc)
        self.assertIn("error=", loc)
        self.assertNotIn("/test-matrix-runs/", loc)   # no run was queued


if __name__ == "__main__":
    unittest.main()
