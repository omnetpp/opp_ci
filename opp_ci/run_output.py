"""
In-memory live stage buffers on the coordinator.

While a run executes, the worker streams **stage events** here (see
opp_ci.stages / opp_ci.worker._RunOutputStreamer) so the run-detail page can
live-tail its stages (deps.install, project.build, test.run, …) and their
output. Once the run finishes, the authoritative result lives on the TestRun
row (and, later, TestRunStage); the live buffer is dropped.

Bounded per run, and the number of in-flight runs tracked is capped (LRU).
Purely in-memory, lost on restart.

Events applied (see opp_ci.stages for the shapes):
  stage_begin → create/replace the stage entry, mark it the open stage
  output      → append a line (tagged with the open stage's ordinal + stream)
  stage_end   → update the stage's status + exit

The store assigns a monotonic per-run ``seq`` to output lines; the run-detail
tail uses it as the opaque cursor. The stage list (small: name/status/exit)
is always returned in full, so a viewer that joins late still sees every
stage header even if early output lines have been evicted from the ring.
"""

import threading
from collections import OrderedDict, deque

from opp_ci import config as cfg


class RunOutputStore:
    """Bounded per-run stage + output buffer, keyed by run id."""

    def __init__(self, ring, max_runs):
        self._ring = ring
        self._max_runs = max_runs
        self._runs = OrderedDict()   # run_id -> run state dict (LRU order)
        self._lock = threading.Lock()

    def _run_state(self, run_id):
        st = self._runs.get(run_id)
        if st is None:
            st = {
                "stages": {},                       # ordinal -> stage dict
                "lines": deque(maxlen=self._ring),  # {seq, ordinal, stream, text}
                "seq": 1,
                "open": None,                       # open stage ordinal
            }
            self._runs[run_id] = st
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)       # evict LRU run
        self._runs.move_to_end(run_id)
        return st

    def append(self, run_id, events):
        """Apply a batch of ordered stage events for a run."""
        if not events:
            return
        with self._lock:
            st = self._run_state(run_id)
            for ev in events:
                kind = ev.get("kind")
                if kind == "stage_begin":
                    ordinal = ev.get("ordinal")
                    st["stages"][ordinal] = {
                        "ordinal": ordinal,
                        "name": ev.get("stage"),
                        "command": ev.get("command"),
                        "status": "running",
                        "exit": None,
                    }
                    st["open"] = ordinal
                elif kind == "output":
                    st["lines"].append({
                        "seq": st["seq"],
                        "ordinal": st["open"],
                        "stream": ev.get("stream", "out"),
                        "text": ev.get("text", ""),
                    })
                    st["seq"] += 1
                elif kind == "stage_end":
                    # Match the open stage (events are ordered); fall back to
                    # the newest stage with this name.
                    stage = st["stages"].get(st["open"])
                    if stage is None or stage.get("name") != ev.get("stage"):
                        stage = next((s for s in reversed(list(st["stages"].values()))
                                      if s["name"] == ev.get("stage")), None)
                    if stage is not None:
                        stage["status"] = ev.get("status")
                        stage["exit"] = ev.get("exit")

    def snapshot(self, run_id, after_seq):
        """Return (stages, new_lines, last_seq) for a run.

        stages: full list, ordered by ordinal. new_lines: output lines with
        ``seq > after_seq``. last_seq: newest line seq, or ``after_seq`` when
        nothing is newer (so the cursor never rewinds).
        """
        with self._lock:
            st = self._runs.get(run_id)
            if st is None:
                return [], [], after_seq
            stages = [dict(s) for _, s in sorted(st["stages"].items())]
            new_lines = [dict(l) for l in st["lines"] if l["seq"] > (after_seq or 0)]
        last_seq = new_lines[-1]["seq"] if new_lines else after_seq
        return stages, new_lines, last_seq

    def has(self, run_id):
        with self._lock:
            return run_id in self._runs

    def drop(self, run_id):
        with self._lock:
            self._runs.pop(run_id, None)


# Process-wide store used by the serve process. Sized from config at import.
STORE = RunOutputStore(cfg.SERVE_RUN_OUTPUT_RING, cfg.SERVE_RUN_OUTPUT_MAX_RUNS)
