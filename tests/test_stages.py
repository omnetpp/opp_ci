"""Tests for the stage model + recorder (plan/pending/staged-execution-capture.md).

Run with: python -m unittest tests.test_stages
"""

import unittest

from opp_ci.stages import (
    Stage, StageRecorder, PASSED, FAILED, SKIPPED, RUNNING, status_for_exit)


class StatusForExitTests(unittest.TestCase):
    def test_zero_passes_nonzero_fails(self):
        self.assertEqual(status_for_exit(0), PASSED)
        self.assertEqual(status_for_exit(1), FAILED)
        self.assertEqual(status_for_exit(2), FAILED)


class StageRecorderTests(unittest.TestCase):
    def test_builds_tree_with_status_from_exit(self):
        rec = StageRecorder()
        rec.begin(Stage.PROJECT_BUILD, command="opp_build_project")
        rec.output("out", "compiling…")
        rec.end(0)
        self.assertEqual(len(rec.stages), 1)
        st = rec.stages[0]
        self.assertEqual(st["name"], Stage.PROJECT_BUILD)
        self.assertEqual(st["ordinal"], 0)
        self.assertEqual(st["status"], PASSED)
        self.assertEqual(st["exit"], 0)
        self.assertEqual(st["output"], [{"stream": "out", "text": "compiling…"}])
        self.assertIsNotNone(st["finished_at"])

    def test_failed_exit_marks_failed(self):
        rec = StageRecorder()
        rec.begin(Stage.PROJECT_BUILD)
        rec.end(2)
        self.assertEqual(rec.stages[0]["status"], FAILED)

    def test_ordinals_increment(self):
        rec = StageRecorder()
        rec.begin(Stage.DEPS_INSTALL); rec.end(0)
        rec.begin(Stage.PROJECT_BUILD); rec.end(0)
        rec.begin(Stage.TEST_RUN); rec.end(0)
        self.assertEqual([s["ordinal"] for s in rec.stages], [0, 1, 2])

    def test_output_attributed_to_open_stage(self):
        rec = StageRecorder()
        rec.begin(Stage.DEPS_INSTALL)
        rec.output("err", "warn")
        rec.end(0)
        rec.begin(Stage.TEST_RUN)
        rec.output("out", "running")
        rec.end(0)
        self.assertEqual(rec.stages[0]["output"], [{"stream": "err", "text": "warn"}])
        self.assertEqual(rec.stages[1]["output"], [{"stream": "out", "text": "running"}])

    def test_emits_events_in_order(self):
        events = []
        rec = StageRecorder(on_event=events.append)
        rec.begin(Stage.TEST_RUN, command="cmd")
        rec.output("out", "line1")
        rec.end(0)
        self.assertEqual([e["kind"] for e in events],
                         ["stage_begin", "output", "stage_end"])
        self.assertEqual(events[0]["command"], "cmd")
        self.assertEqual(events[1]["stream"], "out")
        self.assertEqual(events[2]["status"], PASSED)

    def test_skip_records_skipped_stage_and_events(self):
        events = []
        rec = StageRecorder(on_event=events.append)
        rec.skip(Stage.TEST_RUN, reason="build failed")
        st = rec.stages[0]
        self.assertEqual(st["status"], SKIPPED)
        self.assertEqual(st["output"], [{"stream": "err", "text": "build failed"}])
        self.assertEqual([e["kind"] for e in events], ["stage_begin", "stage_end"])
        self.assertEqual(events[-1]["status"], SKIPPED)

    def test_explicit_status_override(self):
        rec = StageRecorder()
        rec.begin(Stage.TEST_RUN)
        rec.end(exit_code=None, status=RUNNING)
        self.assertEqual(rec.stages[0]["status"], RUNNING)

    def test_on_event_exception_swallowed(self):
        def boom(_):
            raise RuntimeError("nope")
        rec = StageRecorder(on_event=boom)
        rec.begin(Stage.TEST_RUN)   # must not raise
        rec.output("out", "x")
        rec.end(0)
        self.assertEqual(rec.stages[0]["status"], PASSED)


if __name__ == "__main__":
    unittest.main()
