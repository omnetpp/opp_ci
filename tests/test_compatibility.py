"""Tests for the compatibility matrix data layer (opp_ci.compatibility).

Covers the two-channel cell encoding and the dimension filters:
  * status channel aggregates result_code by homogeneity (all-same -> that
    code, disagree -> "mixed"), NOT the old precedence logic
  * verdict channel aggregates recorded_verdict the same way; a run with no
    recorded verdict folds into UNKNOWN
  * filters subset the empirical overlay; a declared cell with no matching
    run reverts to "compatible" (verdict None)
  * scoped `options` list only the dimension values present for the project

Run with: python -m unittest tests.test_compatibility   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_compat_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["OPP_CI_REMOTE"] = "0"

from opp_ci.db.connection import engine, SessionLocal            # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    Base, Project, TestResultCode, TestRunLifecycle, Version,
)
from opp_ci.persistence import (                                 # noqa: E402
    create_test_run, finalize_verdict_for_run, get_or_create_test,
    insert_expectation,
)
from opp_ci.compatibility import get_compatibility_matrix        # noqa: E402

VLABEL = "inet-4.5"
DEPVER = "6.1"


def _coord(**over):
    base = {"project": "inet", "kind": "smoke", "mode": "release", "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


class CompatibilityMatrixTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

        project = Project(name="inet", dependency_names=["omnetpp"])
        self.s.add(project)
        self.s.flush()
        self.s.add(Version(project_id=project.id, opp_env_version=VLABEL,
                           resolved_dependencies={"omnetpp": [DEPVER]}))
        self.s.flush()

    def tearDown(self):
        self.s.close()

    def _add_run(self, result_code, *, expectation=None, finalize=True,
                 git_ref=None, deps={"omnetpp": DEPVER}, **coord):
        """Add a finished run for the (inet-4.5, omnetpp 6.1) cell.

        By default the run carries NO version/git_ref -- only Test.project and
        the dep pin -- mirroring `opp_ci --remote run --project <p> --pin
        omnetpp=...`. Attribution must come from the pin alone, never the
        version/ref. `git_ref`/`deps` can be overridden to probe that.
        """
        test = get_or_create_test(self.s, _coord(**coord))
        if expectation is not None:
            insert_expectation(self.s, test_id=test.id,
                               expected_result_code=expectation)
        run = create_test_run(self.s, test_id=test.id,
                              git_ref=git_ref, resolved_deps=deps)
        run.lifecycle = TestRunLifecycle.finished
        run.result_code = result_code
        run.finished_at = datetime.datetime(2026, 1, 1)
        self.s.flush()
        if finalize:
            finalize_verdict_for_run(self.s, run.id)
        return run

    def _cell(self, filters=None):
        result = get_compatibility_matrix(self.s, "inet", filters)
        matrix = result["matrices"][0]
        self.assertEqual(matrix["dependency"], "omnetpp")
        return matrix["rows"][0]["cells"][DEPVER]

    def _options(self, filters=None):
        return get_compatibility_matrix(self.s, "inet", filters)["options"]

    # ── status channel ────────────────────────────────────────────────

    def test_unfiltered_disagreeing_runs_are_mixed(self):
        self._add_run(TestResultCode.PASS, distro="ubuntu", compiler="gcc")
        self._add_run(TestResultCode.FAIL, distro="fedora", compiler="clang")
        cell = self._cell()
        # Homogeneity, not precedence: PASS+FAIL -> mixed (not FAIL).
        self.assertEqual(cell["status"], "mixed")

    def test_filter_collapses_to_single_run_status(self):
        self._add_run(TestResultCode.PASS, distro="ubuntu", compiler="gcc")
        self._add_run(TestResultCode.FAIL, distro="fedora", compiler="clang")
        self.assertEqual(self._cell({"compiler": "gcc"})["status"], "PASS")
        self.assertEqual(self._cell({"compiler": "clang"})["status"], "FAIL")

    def test_all_same_status_keeps_that_status(self):
        self._add_run(TestResultCode.PASS, distro="ubuntu", compiler="gcc")
        self._add_run(TestResultCode.PASS, distro="fedora", compiler="clang")
        self.assertEqual(self._cell()["status"], "PASS")

    def test_filtered_away_declared_cell_is_compatible(self):
        self._add_run(TestResultCode.PASS, os="Linux", compiler="gcc")
        # No run on Windows -> declared cell reverts to compatible, no verdict.
        cell = self._cell({"os": "Windows"})
        self.assertEqual(cell["status"], "compatible")
        self.assertIsNone(cell["verdict"])
        self.assertEqual(cell["runs"], [])

    def test_attributed_by_pin_not_by_version_or_ref(self):
        # The mm1k regression: a run whose git_ref does NOT match the version
        # label ("inet-4.5") must still overlay -- the overlay filters only on
        # visible dimensions, never on the hidden version/ref.
        self._add_run(TestResultCode.PASS, git_ref="some-unrelated-branch",
                      compiler="gcc")
        self.assertEqual(self._cell()["status"], "PASS")

    def test_run_without_dep_pin_is_not_overlaid(self):
        # No resolved_deps -> no column coordinate -> cell stays compatible.
        self._add_run(TestResultCode.PASS, deps=None, compiler="gcc")
        cell = self._cell()
        self.assertEqual(cell["status"], "compatible")
        self.assertEqual(cell["runs"], [])

    # ── verdict channel ───────────────────────────────────────────────

    def test_all_expected_is_expected(self):
        self._add_run(TestResultCode.PASS, expectation=TestResultCode.PASS,
                      distro="ubuntu", compiler="gcc")
        self._add_run(TestResultCode.PASS, expectation=TestResultCode.PASS,
                      distro="fedora", compiler="clang")
        self.assertEqual(self._cell()["verdict"], "EXPECTED")

    def test_disagreeing_verdicts_are_mixed(self):
        self._add_run(TestResultCode.PASS, expectation=TestResultCode.PASS,
                      distro="ubuntu", compiler="gcc")           # EXPECTED
        self._add_run(TestResultCode.FAIL, expectation=TestResultCode.PASS,
                      distro="fedora", compiler="clang")          # UNEXPECTED
        self.assertEqual(self._cell()["verdict"], "mixed")

    def test_no_recorded_verdict_folds_into_unknown(self):
        # A finished run with no finalized verdict -> recorded_verdict None,
        # which the verdict aggregator treats as UNKNOWN.
        self._add_run(TestResultCode.PASS, finalize=False, compiler="gcc")
        self.assertEqual(self._cell()["verdict"], "UNKNOWN")

    # ── scoped options ────────────────────────────────────────────────

    def test_options_list_only_project_dimension_values(self):
        self._add_run(TestResultCode.PASS, os="Linux", distro="ubuntu", compiler="gcc")
        self._add_run(TestResultCode.FAIL, os="Linux", distro="fedora", compiler="clang")
        opts = self._options()
        self.assertEqual(opts["os"], ["Linux"])
        self.assertEqual(opts["distro"], ["fedora", "ubuntu"])
        self.assertEqual(opts["compiler"], ["clang", "gcc"])
        self.assertEqual(opts["mode"], ["release"])
        # A dimension never set on any run yields an empty option list.
        self.assertEqual(opts["arch"], [])

    def test_empty_filter_matches_unfiltered(self):
        self._add_run(TestResultCode.PASS, compiler="gcc")
        self.assertEqual(self._cell({}), self._cell(None))


if __name__ == "__main__":
    unittest.main()
