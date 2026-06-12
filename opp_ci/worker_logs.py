"""
In-memory per-worker log buffers on the coordinator.

Remote workers ship their recent log lines on each heartbeat (see
opp_ci.logbuffer / opp_ci.worker); this holds them so the web UI's
per-worker log view can serve workers whose systemd journal lives on
another host. Purely in-memory and bounded per worker — lost on a coordinator
restart, which is acceptable: the next heartbeat refills from the worker's
own ring.

The coordinator assigns its *own* monotonic seq per worker, independent of
the worker's seq, and the log-view tail uses it as the opaque cursor. So a
worker restart (which resets the worker-side seq) doesn't rewind the
viewer's cursor.
"""

import threading
from collections import deque

from opp_ci import config as cfg


class WorkerLogStore:
    """Bounded per-worker ring of shipped log lines, keyed by worker id."""

    def __init__(self, capacity):
        self._capacity = capacity
        self._buffers = {}    # worker_id -> deque[{seq, ts, level, msg}]
        self._next_seq = {}   # worker_id -> int
        self._lock = threading.Lock()

    def append(self, worker_id, entries):
        """Append shipped ``{ts, level, msg}`` entries, stamping coordinator seqs."""
        if not entries:
            return
        with self._lock:
            buf = self._buffers.get(worker_id)
            if buf is None:
                buf = deque(maxlen=self._capacity)
                self._buffers[worker_id] = buf
                self._next_seq[worker_id] = 1
            seq = self._next_seq[worker_id]
            for e in entries:
                buf.append({
                    "seq": seq,
                    "ts": e.get("ts"),
                    "level": e.get("level"),
                    "msg": e.get("msg", ""),
                })
                seq += 1
            self._next_seq[worker_id] = seq

    def since(self, worker_id, after_seq):
        """Return (entries, last_seq) for records with ``seq > after_seq``.

        last_seq is the seq of the newest returned entry, or the passed-in
        ``after_seq`` when nothing is newer — so the viewer's cursor never
        goes backwards.
        """
        with self._lock:
            buf = self._buffers.get(worker_id)
            fresh = [dict(e) for e in buf if e["seq"] > (after_seq or 0)] if buf else []
        last_seq = fresh[-1]["seq"] if fresh else after_seq
        return fresh, last_seq

    def has(self, worker_id):
        """True once a worker has shipped at least one line."""
        with self._lock:
            return bool(self._buffers.get(worker_id))


# Process-wide store used by the coordinator process. Sized from config at import.
STORE = WorkerLogStore(cfg.COORDINATOR_WORKER_LOG_RING)
