"""Tests for per-run verdicts (nullable TestVerdict.matrix_run_id).

Covers persistence.finalize_verdict_for_run giving every finished run its
own verdict:
  * standalone run with no expectation  -> UNKNOWN, matrix_run_id NULL
  * standalone run vs matching / mismatching expectation -> EXPECTED / UNEXPECTED
  * snapshot semantics: editing the expectation afterwards does not change
    the recorded verdict
  * idempotency: finalize twice -> exactly one verdict row
  * matrix run keeps only its matrix cell (no extra NULL-matrix row)
  * unfinished / no-result run -> no verdict row

Run with: python -m unittest tests.test_per_run_verdict   (no pytest needed)
"""

import datetime
import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_verdict_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["OPP_CI_REMOTE"] = "0"

from opp_ci.db.connection import engine, SessionLocal            # noqa: E402
from opp_ci.db.models import (                                   # noqa: E402
    Base, TestMatrix, TestResultCode, TestRunLifecycle, TestVerdict,
    TestVerdictKind,
)
from opp_ci.persistence import (                                 # noqa: E402
    USE_GLOBAL_DEFAULT, create_matrix_run, create_test_run, create_test_verdict,
    finalize_verdict_for_run, get_current_expectation, get_or_create_test,
    insert_expectation, parse_expectation_override,
    read_default_expectation_code, set_default_expectation_code,
)


def _coord(**over):
    base = {"project": "inet", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


class PerRunVerdictTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _finished_standalone(self, code=TestResultCode.PASS, **coord):
        # default_expectation=None: these tests control the expectation
        # explicitly, so suppress the auto-stamped creation default.
        test = get_or_create_test(self.s, _coord(**coord), default_expectation=None)
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=None)
        run.lifecycle = TestRunLifecycle.finished
        run.result_code = code
        run.finished_at = datetime.datetime(2026, 1, 1)
        self.s.flush()
        return test, run

    def _verdicts_for(self, run_id):
        return self.s.query(TestVerdict).filter_by(test_run_id=run_id).all()

    def test_standalone_no_expectation_is_unknown(self):
        _, run = self._finished_standalone()
        finalize_verdict_for_run(self.s, run.id)

        verdicts = self._verdicts_for(run.id)
        self.assertEqual(len(verdicts), 1)
        self.assertIsNone(verdicts[0].matrix_run_id)
        self.assertEqual(verdicts[0].verdict, TestVerdictKind.UNKNOWN)
        self.assertEqual(run.recorded_verdict, "UNKNOWN")

    def test_standalone_matches_expectation_is_expected(self):
        test, run = self._finished_standalone(code=TestResultCode.PASS)
        insert_expectation(self.s, test_id=test.id,
                           expected_result_code=TestResultCode.PASS)
        finalize_verdict_for_run(self.s, run.id)

        self.assertEqual(run.recorded_verdict, "EXPECTED")
        self.assertIsNotNone(self._verdicts_for(run.id)[0].expectation_id)

    def test_standalone_mismatch_is_unexpected(self):
        test, run = self._finished_standalone(code=TestResultCode.FAIL)
        insert_expectation(self.s, test_id=test.id,
                           expected_result_code=TestResultCode.PASS)
        finalize_verdict_for_run(self.s, run.id)

        self.assertEqual(run.recorded_verdict, "UNEXPECTED")

    def test_snapshot_frozen_against_expectation_at_finalize(self):
        test, run = self._finished_standalone(code=TestResultCode.FAIL)
        insert_expectation(self.s, test_id=test.id,
                           expected_result_code=TestResultCode.FAIL)
        finalize_verdict_for_run(self.s, run.id)
        self.assertEqual(run.recorded_verdict, "EXPECTED")

        # Change the expectation after the fact — the recorded verdict is frozen.
        insert_expectation(self.s, test_id=test.id,
                           expected_result_code=TestResultCode.PASS)
        self.assertEqual(run.recorded_verdict, "EXPECTED")

    def test_finalize_is_idempotent(self):
        _, run = self._finished_standalone()
        finalize_verdict_for_run(self.s, run.id)
        finalize_verdict_for_run(self.s, run.id)

        self.assertEqual(len(self._verdicts_for(run.id)), 1)

    def test_matrix_run_keeps_only_its_cell(self):
        test = get_or_create_test(self.s, _coord())
        matrix = TestMatrix(project="inet", config={})
        self.s.add(matrix)
        self.s.flush()
        mr = create_matrix_run(self.s, matrix_id=matrix.id)
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=mr.id)
        # Mimic enqueue_job: the matrix cell exists before the run finishes.
        create_test_verdict(self.s, matrix_run_id=mr.id, test_id=test.id,
                            test_run_id=run.id)
        run.lifecycle = TestRunLifecycle.finished
        run.result_code = TestResultCode.PASS
        run.finished_at = datetime.datetime(2026, 1, 1)
        self.s.flush()

        finalize_verdict_for_run(self.s, run.id)

        verdicts = self._verdicts_for(run.id)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].matrix_run_id, mr.id)

    def test_unfinished_run_gets_no_verdict(self):
        test = get_or_create_test(self.s, _coord())
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=None)
        # still queued, no result_code
        finalize_verdict_for_run(self.s, run.id)

        self.assertEqual(self._verdicts_for(run.id), [])
        self.assertIsNone(run.recorded_verdict)


class DefaultExpectationTests(unittest.TestCase):
    """Auto-stamped default expectation on Test creation."""

    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.s = SessionLocal()

    def tearDown(self):
        self.s.close()

    def _finish(self, test, code):
        run = create_test_run(self.s, test_id=test.id, matrix_run_id=None)
        run.lifecycle = TestRunLifecycle.finished
        run.result_code = code
        run.finished_at = datetime.datetime(2026, 1, 1)
        self.s.flush()
        finalize_verdict_for_run(self.s, run.id)
        return run

    def test_new_test_stamped_with_factory_default_pass(self):
        # No AppSetting row -> factory default PASS, stamped on creation.
        test = get_or_create_test(self.s, _coord())
        exp = get_current_expectation(self.s, test.id)
        self.assertIsNotNone(exp)
        self.assertEqual(exp.expected_result_code, TestResultCode.PASS)
        # First run already yields a verdict — no manual set + re-run needed.
        run = self._finish(test, TestResultCode.PASS)
        self.assertEqual(run.recorded_verdict, "EXPECTED")

    def test_new_test_default_pass_then_failing_run_is_unexpected(self):
        test = get_or_create_test(self.s, _coord())
        run = self._finish(test, TestResultCode.FAIL)
        self.assertEqual(run.recorded_verdict, "UNEXPECTED")

    def test_existing_test_not_restamped(self):
        test = get_or_create_test(self.s, _coord())
        again = get_or_create_test(self.s, _coord())
        self.assertEqual(test.id, again.id)
        rows = test.expected_results
        self.assertEqual(len(rows), 1)  # only the creation default

    def test_override_stamps_given_code(self):
        test = get_or_create_test(self.s, _coord(),
                                  default_expectation=TestResultCode.FAIL)
        exp = get_current_expectation(self.s, test.id)
        self.assertEqual(exp.expected_result_code, TestResultCode.FAIL)

    def test_none_suppresses_stamp(self):
        test = get_or_create_test(self.s, _coord(), default_expectation=None)
        self.assertIsNone(get_current_expectation(self.s, test.id))
        run = self._finish(test, TestResultCode.PASS)
        self.assertEqual(run.recorded_verdict, "UNKNOWN")

    def test_setting_overrides_factory_default(self):
        set_default_expectation_code(self.s, TestResultCode.ERROR)
        self.s.commit()
        self.assertEqual(read_default_expectation_code(self.s), TestResultCode.ERROR)
        test = get_or_create_test(self.s, _coord())
        self.assertEqual(
            get_current_expectation(self.s, test.id).expected_result_code,
            TestResultCode.ERROR,
        )

    def test_cleared_setting_means_no_default(self):
        set_default_expectation_code(self.s, None)  # "no default"
        self.s.commit()
        self.assertIsNone(read_default_expectation_code(self.s))
        test = get_or_create_test(self.s, _coord())
        self.assertIsNone(get_current_expectation(self.s, test.id))

    def test_read_default_unset_is_factory_pass(self):
        self.assertEqual(read_default_expectation_code(self.s), TestResultCode.PASS)

    def test_parse_expectation_override(self):
        self.assertIs(parse_expectation_override(""), USE_GLOBAL_DEFAULT)
        self.assertIs(parse_expectation_override(None), USE_GLOBAL_DEFAULT)
        self.assertEqual(parse_expectation_override("PASS"), TestResultCode.PASS)
        with self.assertRaises(ValueError):
            parse_expectation_override("BOGUS")


if __name__ == "__main__":
    unittest.main()
