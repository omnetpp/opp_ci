"""Tests for the web filter controls & filter completeness work
(plan/pending/web-filter-controls-and-completeness.md).

Covers:
  * the filter helpers — apply_str_filter (eq/contains/prefix), apply_dep_filter,
    matrix_axis_options, matrix_axis_sql_filter (incl. survives-LIMIT correctness)
  * every list page renders, and the new/changed filters narrow results:
    /tests, /test-runs, /results, /test-matrices, /test-matrix-runs, /projects,
    /os, /compilers

Run with: python -m unittest tests.test_filter_controls   (no pytest needed)
"""

import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_filt_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"
os.environ.setdefault("OPP_CI_SESSION_SECRET", "x" * 40)

# config may already have been imported (and frozen) by an earlier test
# module under `unittest discover`, so set the attribute directly too.
from opp_ci import config as _cfg                                # noqa: E402
_cfg.SESSION_SECRET = _cfg.SESSION_SECRET or "x" * 40

from fastapi.testclient import TestClient                       # noqa: E402

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import (                                  # noqa: E402
    Base, Compiler, OS, Project, Test, TestMatrix, TestMatrixRun, TestRun,
    TestRunLifecycle, TestResultCode, User,
)
from opp_ci.web import app as webapp                            # noqa: E402


def _seed():
    s = SessionLocal()
    try:
        s.add(User(id=1, username="tester", role="admin", enabled=True))
        s.add(Project(name="inet", opp_env_name="inet-4.5",
                      github_owner="inet-framework", github_repo="inet",
                      git_url="https://github.com/inet-framework/inet",
                      dependency_names=["omnetpp"]))
        s.add(Project(name="mm1k", github_owner="omnetpp", github_repo="mm1k",
                      dependency_names=["omnetpp", "inet"]))

        # Two tests differing on the version axes so prefix-vs-substring bites.
        t1 = Test(project="inet", kind="smoke", os="Linux", os_version="6.0",
                  compiler="gcc", compiler_version="13.2.0",
                  opp_file="examples/aloha/omnetpp.ini", name="aloha-smoke",
                  resolved_deps={"omnetpp": "6.4.0"},
                  coord_hash="hash-t1")
        t2 = Test(project="mm1k", kind="fingerprint", os="Linux", os_version="16.0",
                  compiler="gcc", compiler_version="13.3.0",
                  opp_file="examples/mm1k/omnetpp.ini", name="mm1k-fp",
                  resolved_deps={"omnetpp": "6.3.0"},
                  coord_hash="hash-t2")
        s.add_all([t1, t2])
        s.flush()

        s.add_all([
            OS(name="Linux", version="22.04", arch="amd64"),
            OS(name="Linux", version="24.04", arch="aarch64"),
            OS(name="Windows", version="11", arch="amd64"),
            Compiler(name="gcc", version="13.2.0"),
            Compiler(name="clang", version="16.0.0"),
        ])

        mx = TestMatrix(name="nightly", project="inet",
                        opp_file="examples/aloha/omnetpp.ini",
                        config={"kinds": ["smoke"], "os": ["Linux"],
                                "compiler_version": ["13.2.0"], "versions": ["6.4.0"]})
        s.add(mx)
        s.flush()
        mxr = TestMatrixRun(matrix_id=mx.id, trigger="web", ref="master",
                            github_owner="inet-framework", github_repo="inet",
                            github_commit_sha="deadbeefcafebabe", github_pr_number=42)
        s.add(mxr)  # smoke matrix run — the OLDEST (lowest id)
        s.flush()

        # A second matrix + several newer runs, so the smoke run above falls
        # outside a small LIMIT window. A correct (SQL) axis filter must still
        # find it; a Python post-filter-after-LIMIT would not.
        mx2 = TestMatrix(name="stress", project="mm1k",
                         config={"kinds": ["fingerprint"], "os": ["Linux"]})
        s.add(mx2)
        s.flush()
        for _ in range(5):
            s.add(TestMatrixRun(matrix_id=mx2.id, trigger="manual"))
        s.flush()

        s.add(TestRun(test_id=t1.id, matrix_run_id=mxr.id, version="6.4.0",
                      lifecycle=TestRunLifecycle.finished,
                      result_code=TestResultCode.PASS, resolved_deps={"omnetpp": "6.4.0"}))
        s.add(TestRun(test_id=t2.id, version="6.3.0",
                      lifecycle=TestRunLifecycle.finished,
                      result_code=TestResultCode.FAIL))
        s.commit()
    finally:
        s.close()


class FilterHelperTests(unittest.TestCase):
    def test_apply_str_filter_modes(self):
        from opp_ci.web.app import apply_str_filter
        s = SessionLocal()
        try:
            from sqlalchemy import select
            # empty value → no-op (both rows)
            q = apply_str_filter(select(Test), Test.os_version, "", "prefix")
            self.assertEqual(len(s.execute(q).scalars().all()), 2)
            # prefix: "6" matches 6.0 but NOT 16.0
            q = apply_str_filter(select(Test), Test.os_version, "6", "prefix")
            vers = sorted(t.os_version for t in s.execute(q).scalars().all())
            self.assertEqual(vers, ["6.0"])
            # contains: "6" matches both 6.0 and 16.0
            q = apply_str_filter(select(Test), Test.os_version, "6", "contains")
            self.assertEqual(len(s.execute(q).scalars().all()), 2)
            # eq: exact
            q = apply_str_filter(select(Test), Test.kind, "smoke")
            self.assertEqual([t.kind for t in s.execute(q).scalars().all()], ["smoke"])
        finally:
            s.close()

    def test_apply_dep_filter(self):
        from opp_ci.web.app import apply_dep_filter
        from sqlalchemy import select
        s = SessionLocal()
        try:
            q = apply_dep_filter(select(Test), Test.resolved_deps, "6.4")
            self.assertEqual([t.name for t in s.execute(q).scalars().all()], ["aloha-smoke"])
            q = apply_dep_filter(select(Test), Test.resolved_deps, "omnetpp")
            self.assertEqual(len(s.execute(q).scalars().all()), 2)
        finally:
            s.close()

    def test_matrix_axis_options(self):
        from opp_ci.web.app import matrix_axis_options
        from sqlalchemy import select
        s = SessionLocal()
        try:
            opts = matrix_axis_options(s.execute(select(TestMatrix)).scalars().all())
            self.assertIn("smoke", opts["kind"])
            self.assertIn("13.2.0", opts["compiler_version"])
        finally:
            s.close()

    def test_matrix_axis_sql_filter(self):
        from opp_ci.web.app import matrix_axis_sql_filter
        from sqlalchemy import select
        s = SessionLocal()
        try:
            dialect = s.bind.dialect.name

            def names(axis_filters):
                q = matrix_axis_sql_filter(select(TestMatrix), axis_filters, dialect)
                return sorted(m.name for m in s.execute(q).scalars().all())

            # exact membership (sel axis)
            self.assertEqual(names({"kind": "smoke"}), ["nightly"])
            self.assertEqual(names({"kind": "nope"}), [])
            self.assertEqual(names({"kind": "fingerprint"}), ["stress"])
            # substring membership, case-insensitive (combo axis)
            self.assertEqual(names({"compiler_version": "13.2"}), ["nightly"])
            self.assertEqual(names({"compiler_version": "99"}), [])
            # exact axis must NOT match on a substring
            self.assertEqual(names({"kind": "smok"}), [])
        finally:
            s.close()


class FilterPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(
            webapp, "_template_globals", wraps=webapp._template_globals)
        # Bypass login: every request resolves to the seeded admin.
        def _load(_uid):
            s = SessionLocal()
            try:
                from sqlalchemy import select
                return s.execute(select(User).where(User.id == 1)).scalar_one_or_none()
            finally:
                s.close()
        cls._auth = mock.patch("opp_ci.auth._load_enabled_user", _load)
        cls._auth.start()
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        cls._auth.stop()

    def _get(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, f"{url} -> {r.status_code}")
        return r.text

    def test_pages_render(self):
        for url in ("/tests", "/test-runs", "/test-runs?view=rollup",
                    "/test-matrices", "/test-matrix-runs", "/projects",
                    "/os", "/compilers"):
            self._get(url)

    def test_matrix_runs_axis_filter_survives_limit(self):
        # The smoke matrix run is the oldest (lowest id); 5 newer fingerprint
        # runs sit above it. With limit=2 the smoke run is outside the window,
        # so a Python post-filter-after-limit would drop it. SQL WHERE-before-
        # LIMIT must still return it.
        body = self._get("/test-matrix-runs?kind=smoke&limit=2")
        self.assertIn('/test-matrix-runs/1"', body)

    def test_tests_filters(self):
        # opp_file combo (substring)
        self.assertIn("aloha-smoke", self._get("/tests?opp_file=aloha"))
        self.assertNotIn("mm1k-fp", self._get("/tests?opp_file=aloha"))
        # os_version prefix: 6 matches 6.0 only
        body = self._get("/tests?os_version=6")
        self.assertIn("aloha-smoke", body)
        self.assertNotIn("mm1k-fp", body)
        # dep filter
        self.assertIn("aloha-smoke", self._get("/tests?dep=6.4"))
        self.assertNotIn("mm1k-fp", self._get("/tests?dep=6.4"))

    def test_runs_filters(self):
        # Assert on row anchors (run #1 = inet/t1, run #2 = mm1k/t2), since
        # project names also appear in the filter datalists.
        # os_version prefix: 6 matches t1's 6.0 but not t2's 16.0
        body = self._get("/test-runs?os_version=6")
        self.assertIn('/test-runs/1"', body)
        self.assertNotIn('/test-runs/2"', body)
        # trigger (via joined matrix run) + github owner — only run #1 has a matrix run
        self.assertIn('/test-runs/1"', self._get("/test-runs?trigger=web"))
        self.assertIn('/test-runs/1"', self._get("/test-runs?github_owner=inet"))
        # standalone run (no matrix run) still shows when no matrix filter set
        self.assertIn('/test-runs/2"', self._get("/test-runs"))

    def test_matrix_runs_axis_and_github(self):
        # matrix dimension search through the joined matrix config. Run #1 is
        # the only smoke run; runs #2-6 are the fingerprint "stress" matrix.
        smoke = self._get("/test-matrix-runs?kind=smoke")
        self.assertIn('/test-matrix-runs/1"', smoke)
        self.assertNotIn('/test-matrix-runs/2"', smoke)
        fp = self._get("/test-matrix-runs?kind=fingerprint")
        self.assertNotIn('/test-matrix-runs/1"', fp)
        self.assertIn('/test-matrix-runs/2"', fp)
        # combo axis substring — only the smoke matrix pins compiler_version
        cv = self._get("/test-matrix-runs?compiler_version=13.2")
        self.assertIn('/test-matrix-runs/1"', cv)
        self.assertNotIn('/test-matrix-runs/2"', cv)
        # github filters (only run #1 carries GitHub context)
        self.assertIn('/test-matrix-runs/1"', self._get("/test-matrix-runs?github_pr_number=42"))
        self.assertIn('/test-matrix-runs/1"', self._get("/test-matrix-runs?github_owner=inet"))
        self.assertIn("No matrix runs found", self._get("/test-matrix-runs?github_owner=nope"))

    def test_matrices_opp_file(self):
        self.assertIn('/test-matrices/1"', self._get("/test-matrices?opp_file=aloha"))
        self.assertIn("No matrices match", self._get("/test-matrices?opp_file=zzz"))

    def test_matrices_last_status(self):
        # Both matrices' latest runs have completed_at NULL -> "pending".
        body = self._get("/test-matrices?status=pending")
        self.assertIn('/test-matrices/1"', body)   # nightly
        self.assertIn('/test-matrices/2"', body)   # stress
        # No matrix run has finished with a PASS summary yet.
        self.assertIn("No matrices match", self._get("/test-matrices?status=PASS"))

    def test_runs_result_model(self):
        # Actual (outcome): run #1 PASS, run #2 FAIL.
        pass_body = self._get("/test-runs?actual=PASS")
        self.assertIn('/test-runs/1"', pass_body)
        self.assertNotIn('/test-runs/2"', pass_body)
        fail_body = self._get("/test-runs?actual=FAIL")
        self.assertIn('/test-runs/2"', fail_body)
        self.assertNotIn('/test-runs/1"', fail_body)
        # State (lifecycle): both runs are finished.
        finished = self._get("/test-runs?state=finished")
        self.assertIn('/test-runs/1"', finished)
        self.assertIn('/test-runs/2"', finished)
        self.assertNotIn('/test-runs/1"', self._get("/test-runs?state=queued"))
        # Verdict filter is wired (no promoted verdicts seeded -> empty, not 500).
        none = self._get("/test-runs?verdict=EXPECTED")
        self.assertNotIn('/test-runs/1"', none)
        self.assertNotIn('/test-runs/2"', none)

    def test_runs_ref_and_commit(self):
        # Split Git-ref control: ref->git_ref, commit->commit_sha (seeded NULL,
        # so any value narrows to nothing) — exercises both code paths.
        self.assertNotIn('/test-runs/1"', self._get("/test-runs?ref=master"))
        self.assertNotIn('/test-runs/1"', self._get("/test-runs?commit=deadbeef"))

    def test_runs_rollup_view(self):
        # The former Results summary is now /test-runs?view=rollup. Its rows
        # drill down to the flat list of their run_ids, and resolved_deps (where
        # the omnetpp version lives) is its own column — run #1 pins omnetpp 6.4.0.
        body = self._get("/test-runs?view=rollup")
        self.assertIn("omnetpp=6.4.0", body)
        self.assertIn("run_ids=", body)                 # drill-down link to flat list
        # Rollup honours the same filters (scoping the rolled-up input set).
        self.assertIn("omnetpp=6.4.0", self._get("/test-runs?view=rollup&project=inet"))
        self.assertNotIn("omnetpp=6.4.0", self._get("/test-runs?view=rollup&project=nope"))

    def test_results_redirects_to_runs(self):
        # /results is retired: it redirects to the merged page, defaulting to
        # the rollup view, and keeps the legacy view names working.
        r = self.client.get("/results", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertIn("/test-runs", r.headers["location"])
        self.assertIn("view=summary", r.headers["location"])
        r2 = self.client.get("/results?run_ids=1&view=detailed", follow_redirects=False)
        self.assertIn("run_ids=1", r2.headers["location"])
        self.assertIn("view=detailed", r2.headers["location"])  # runs_list maps -> flat

    def test_projects_filters(self):
        body = self._get("/projects?github_owner=inet-framework")
        self.assertIn('/projects/inet"', body)
        self.assertNotIn('/projects/mm1k"', body)
        # dependency substring (JSON list): only mm1k depends on inet
        body = self._get("/projects?dep=inet")
        self.assertIn('/projects/mm1k"', body)
        self.assertNotIn('/projects/inet"', body)

    def test_os_filters(self):
        # name (exact select) + version (prefix combo)
        body = self._get("/os?name=Linux")
        self.assertIn('/os/1"', body)            # Linux 22.04
        self.assertNotIn('/os/3"', body)         # Windows 11
        # version prefix: "22" matches 22.04, not 11 / 24.04
        body = self._get("/os?version=22")
        self.assertIn('/os/1"', body)
        self.assertNotIn('/os/2"', body)
        # arch exact select
        self.assertIn('/os/2"', self._get("/os?arch=aarch64"))

    def test_compilers_filters(self):
        # version prefix: "13" matches gcc 13.2.0, not clang 16.0.0
        body = self._get("/compilers?version=13")
        self.assertIn('/compilers/1"', body)
        self.assertNotIn('/compilers/2"', body)
        self.assertIn('/compilers/2"', self._get("/compilers?name=clang"))


def setUpModule():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _seed()


if __name__ == "__main__":
    unittest.main()
