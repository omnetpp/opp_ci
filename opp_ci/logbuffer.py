"""
In-memory ring-buffer logging handler for the worker.

A worker on a different host than the coordinator logs to *that* host's
systemd journal, which the coordinator can't read — so the web UI's
per-worker log view can't show it. To fix that, the worker keeps its
recent log records in this ring buffer and ships the new ones to the
coordinator on each heartbeat (see opp_ci.worker), which serves them to
the log view from an in-memory store (see opp_ci.worker_logs).

Capturing through a logging.Handler rather than shelling out to
``journalctl`` needs no journal read access and works on non-systemd
hosts. It also picks up the executor's streamed subprocess output for
free: ``_run_external_streaming`` tees every child line via
``_logger.info``, so build/compile/test progress flows through this
handler like any other record.

Records carry a process-local monotonic ``seq``. The worker remembers the
seq of the last batch it shipped and asks for records strictly newer, so
heartbeats never re-send or duplicate lines — the same after-cursor idea
journald uses, with a plain integer instead of an opaque cursor.
"""

import logging
import threading
from collections import deque


class RingBufferHandler(logging.Handler):
    """Keep the last ``capacity`` log records in memory, newest-seq last."""

    def __init__(self, capacity):
        super().__init__()
        self._buf = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_seq = 1

    def emit(self, record):
        # Format outside the lock; formatting can run user __str__ code and
        # we don't want that holding up other threads' logging.
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 — mirror logging.Handler.emit
            self.handleError(record)
            return
        with self._lock:
            self._buf.append({
                "seq": self._next_seq,
                "ts": record.created,   # epoch seconds (float)
                "level": record.levelno,
                "msg": msg,
            })
            self._next_seq += 1

    def since(self, after_seq, limit=None):
        """Return (entries, dropped) for records with ``seq > after_seq``.

        entries: oldest→newest, copies safe to hand off. When more than
        ``limit`` match, the most-recent ``limit`` are kept and ``dropped``
        counts the older ones skipped — so the caller can show a gap marker
        rather than silently losing lines.
        """
        with self._lock:
            fresh = [dict(e) for e in self._buf if e["seq"] > (after_seq or 0)]
        dropped = 0
        if limit is not None and len(fresh) > limit:
            dropped = len(fresh) - limit
            fresh = fresh[-limit:]
        return fresh, dropped
