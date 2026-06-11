"""
Stage model and recorder for staged execution capture.

A run's work is captured as an ordered list of stages — container prepare,
bootstrap, dependency install, compilation, test run, cleanup. The executor
drives the stages and feeds a StageRecorder, which both:

  * emits live events to a sink (shipped to the coordinator for the
    run-detail live view), and
  * accumulates the assembled stage tree for the final result report.

Events are plain dicts so they serialise straight to JSON for transport:

  {"kind": "stage_begin", "stage": name, "ordinal": int, "command": str|None}
  {"kind": "output",      "stage": name, "stream": "out"|"err"|"cmd", "text": str}
  {"kind": "stage_end",   "stage": name, "exit": int|None, "status": str}

Plan: plan/pending/staged-execution-capture.md.
"""

import time


class Stage:
    """Canonical stage names, in display order (see STAGE_ORDER)."""

    CONTAINER_PREPARE = "container.prepare"
    RUNNER_BOOTSTRAP = "runner.bootstrap"
    CHECKOUT = "checkout"
    DEPS_INSTALL = "deps.install"
    PROJECT_BUILD = "project.build"
    TEST_RUN = "test.run"
    CLEANUP = "cleanup"


STAGE_ORDER = [
    Stage.CONTAINER_PREPARE, Stage.RUNNER_BOOTSTRAP, Stage.CHECKOUT,
    Stage.DEPS_INSTALL, Stage.PROJECT_BUILD, Stage.TEST_RUN, Stage.CLEANUP,
]

# Stage status values.
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"


def status_for_exit(exit_code):
    """passed when exit_code == 0, else failed."""
    return PASSED if exit_code == 0 else FAILED


class StageRecorder:
    """Builds the event stream + assembled stage tree for one run.

    The executor calls begin()/output()/end() around each stage's command(s).
    Each call emits a live event to ``on_event`` (best-effort — a sink hiccup
    must never break the run) and updates the in-memory ``stages`` tree used
    for the final report. Ordinals are assigned in call order.
    """

    def __init__(self, on_event=None):
        self._on_event = on_event
        self.stages = []      # assembled list of stage dicts
        self._open = None     # the currently-open stage dict, or None

    def _emit(self, event):
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 — live streaming is best-effort
            pass

    def begin(self, name, command=None):
        """Open a stage. Closes any still-open stage defensively."""
        ordinal = len(self.stages)
        stage = {
            "name": name, "ordinal": ordinal, "command": command,
            "status": RUNNING, "exit": None, "output": [],
            "started_at": time.time(), "finished_at": None,
        }
        self.stages.append(stage)
        self._open = stage
        self._emit({"kind": "stage_begin", "stage": name,
                    "ordinal": ordinal, "command": command})

    def output(self, stream, text):
        """Record an output line on the open stage. ``stream`` is
        "out" | "err" | "cmd"."""
        name = self._open["name"] if self._open else None
        if self._open is not None:
            self._open["output"].append({"stream": stream, "text": text})
        self._emit({"kind": "output", "stage": name,
                    "stream": stream, "text": text})

    def end(self, exit_code=None, status=None):
        """Close the open stage; status defaults to passed/failed by exit."""
        status = status or status_for_exit(exit_code)
        name = self._open["name"] if self._open else None
        if self._open is not None:
            self._open["status"] = status
            self._open["exit"] = exit_code
            self._open["finished_at"] = time.time()
        self._emit({"kind": "stage_end", "stage": name,
                    "exit": exit_code, "status": status})
        self._open = None
        return status

    def skip(self, name, command=None, reason=None):
        """Record a stage that never ran (e.g. aborted after an earlier
        failure) so the UI shows it greyed rather than simply missing."""
        ordinal = len(self.stages)
        stage = {"name": name, "ordinal": ordinal, "command": command,
                 "status": SKIPPED, "exit": None, "output": [],
                 "started_at": None, "finished_at": None}
        if reason:
            stage["output"].append({"stream": "err", "text": reason})
        self.stages.append(stage)
        self._emit({"kind": "stage_begin", "stage": name,
                    "ordinal": ordinal, "command": command})
        self._emit({"kind": "stage_end", "stage": name,
                    "exit": None, "status": SKIPPED})
