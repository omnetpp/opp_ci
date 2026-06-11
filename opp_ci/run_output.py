"""
In-memory live test-output buffers on the coordinator.

While a run executes, the worker streams the test command's stdout/stderr
here (see opp_ci.worker._RunOutputStreamer) so the run-detail page can
live-tail it. Once the run finishes, the authoritative output lives on the
TestRun row and the page renders that instead — the live buffer is dropped.

Bounded per run, and the number of in-flight runs tracked is capped (LRU).
Purely in-memory, lost on restart — the same trade made for worker log
shipping (see opp_ci.worker_logs).

The coordinator assigns its own monotonic seq per run; the run-detail tail
uses it as the opaque cursor.
"""

import threading
from collections import OrderedDict, deque

from opp_ci import config as cfg


class RunOutputStore:
    """Bounded per-run ring of streamed output lines, keyed by run id."""

    def __init__(self, ring, max_runs):
        self._ring = ring
        self._max_runs = max_runs
        self._runs = OrderedDict()   # run_id -> deque[{seq, text}]  (LRU order)
        self._seq = {}               # run_id -> int
        self._lock = threading.Lock()

    def append(self, run_id, lines):
        """Append output lines for a run, stamping monotonic serve seqs."""
        if not lines:
            return
        with self._lock:
            buf = self._runs.get(run_id)
            if buf is None:
                buf = deque(maxlen=self._ring)
                self._runs[run_id] = buf
                self._seq[run_id] = 1
                # Cap the number of tracked runs; evict the least-recently-used.
                while len(self._runs) > self._max_runs:
                    old_id, _ = self._runs.popitem(last=False)
                    self._seq.pop(old_id, None)
            self._runs.move_to_end(run_id)
            seq = self._seq[run_id]
            for line in lines:
                buf.append({"seq": seq, "text": line})
                seq += 1
            self._seq[run_id] = seq

    def since(self, run_id, after_seq):
        """Return (entries, last_seq) for lines with ``seq > after_seq``.

        last_seq is the newest returned seq, or the passed-in ``after_seq``
        when nothing is newer — so the viewer's cursor never rewinds.
        """
        with self._lock:
            buf = self._runs.get(run_id)
            fresh = [dict(e) for e in buf if e["seq"] > (after_seq or 0)] if buf else []
        last_seq = fresh[-1]["seq"] if fresh else after_seq
        return fresh, last_seq

    def has(self, run_id):
        """True once a run has streamed at least one line."""
        with self._lock:
            return bool(self._runs.get(run_id))

    def drop(self, run_id):
        """Discard a run's buffer (called when the run finishes)."""
        with self._lock:
            self._runs.pop(run_id, None)
            self._seq.pop(run_id, None)


# Process-wide store used by the serve process. Sized from config at import.
STORE = RunOutputStore(cfg.SERVE_RUN_OUTPUT_RING, cfg.SERVE_RUN_OUTPUT_MAX_RUNS)
