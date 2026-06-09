"""
Read process logs from systemd-journald for the web UI's Logs pages.

`opp_ci serve` and each `opp_ci worker start` run as system units
(`opp_ci-serve.service`, `opp_ci-worker@<name>.service`), and systemd
already captures their full stdout/stderr into the journal — per unit,
with rotation and retention. So the Logs pages don't add any capture
layer; they just shell out to `journalctl -o json` and render.

journald exposes an opaque per-entry `__CURSOR`. The viewer holds the
last cursor and polls with `--after-cursor`, which returns only newer
entries — rotation-safe, no byte-offset bookkeeping.

Reading another unit's journal as a non-root user requires journal read
access; the serve unit gets it via `SupplementaryGroups=systemd-journal`.
Without it, `journalctl` fails and `read_unit` reports `available=False`
with a reason rather than an empty pane.
"""

import datetime
import json
import logging
import shutil
import subprocess

from opp_ci import config as cfg

_logger = logging.getLogger(__name__)

# Characters allowed in a worker name we're willing to turn into a systemd
# instance. The deployment uses the name verbatim as the instance
# (opp_ci-worker@<name>), so anything outside this set could never be a
# valid unit here anyway — reject rather than query a bogus unit.
_SAFE_INSTANCE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


class JournalUnavailable(Exception):
    """Raised when the journal can't be read; carries a human reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def worker_unit_name(name):
    """systemd unit for a worker, e.g. 'opp_ci-worker@local.service'.

    The name is used verbatim as the instance, matching the deployment.
    Raises JournalUnavailable if the name isn't instance-safe.
    """
    if not name or any(c not in _SAFE_INSTANCE for c in name):
        raise JournalUnavailable(
            f"Worker name {name!r} is not a valid systemd instance name")
    return cfg.WORKER_UNIT_TEMPLATE.format(instance=name)


def _decode_message(value):
    """journald MESSAGE is usually a str, but non-UTF-8 lines arrive as a
    list of byte values. Normalise both to text."""
    if isinstance(value, list):
        try:
            return bytes(value).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""
    if value is None:
        return ""
    return str(value)


def _parse_entry(obj):
    """One journald JSON object → {ts, priority, message, cursor}."""
    ts = None
    raw_ts = obj.get("__REALTIME_TIMESTAMP")
    if raw_ts is not None:
        try:
            ts = datetime.datetime.fromtimestamp(
                int(raw_ts) / 1_000_000, tz=datetime.timezone.utc)
        except (ValueError, TypeError, OverflowError):
            ts = None
    try:
        priority = int(obj.get("PRIORITY", 6))
    except (ValueError, TypeError):
        priority = 6
    return {
        "ts": ts,
        "priority": priority,
        "message": _decode_message(obj.get("MESSAGE")),
        "cursor": obj.get("__CURSOR"),
    }


def read_unit(unit, *, cursor=None, lines=None, timeout=10):
    """Return (entries, last_cursor) for a systemd unit's journal.

    - cursor None  → the last `lines` entries (initial load).
    - cursor given → entries strictly after it (incremental poll); an
      empty result (no new lines) is normal.

    entries: list of {ts, priority, message, cursor}.
    last_cursor: cursor of the last entry, or the passed-in cursor when
    nothing new arrived (so the caller's poll position never goes
    backwards).

    Raises JournalUnavailable when journalctl is missing, errors, or is
    denied — the caller turns that into available=False + reason.
    """
    if lines is None:
        lines = cfg.LOG_TAIL_LINES
    if shutil.which("journalctl") is None:
        raise JournalUnavailable(
            "journalctl not found — log viewing requires systemd-journald")

    cmd = ["journalctl", "--no-pager", "--output=json", "--unit", unit]
    if cursor:
        # --after-cursor returns entries strictly newer than the cursor.
        cmd += ["--after-cursor", cursor]
    else:
        cmd += ["--lines", str(int(lines))]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise JournalUnavailable("journalctl timed out")
    except OSError as e:
        raise JournalUnavailable(f"could not run journalctl: {e}")

    if proc.returncode != 0:
        # Most common cause: the serve process lacks journal read access
        # (no systemd-journal supplementary group).
        detail = (proc.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"journalctl exited {proc.returncode}"
        raise JournalUnavailable(reason)

    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(_parse_entry(obj))

    last_cursor = entries[-1]["cursor"] if entries else cursor
    return entries, last_cursor
