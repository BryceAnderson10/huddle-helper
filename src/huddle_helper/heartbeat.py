"""The heartbeat (#40, #63): a fresh beat means a process holds the meeting (ADR 0002).

While a meeting records, a thread of its own rewrites ``heartbeat.json`` in the meeting folder every
``INTERVAL_S`` seconds, whatever the audio does: in total silence and while the mic is muted it beats
just the same. It stops when Stop has set ``meta.json`` to ``processing``; a crash stops it too, and
``meta.json`` then stays at ``recording`` until the app next starts. The final pass beats again while it
runs (``Heartbeat``), and the tray beats for every meeting in its final pass queue (``Heartbeats``, one
thread for all of them), so recover skips any meeting with a fresh beat, whatever its status. A meeting's
heartbeat state (``status.heartbeat_state``) is:

    ACTIVE         status ``recording`` and the last beat at most ``STALE_AFTER_S`` old: recording,
                   even if nobody has spoken for an hour (transcript freshness is not health)
    RECORDER_DIED  status ``recording`` but the beat is stale, missing or unreadable
    NOT_RECORDING  any other status: stopped, in its final pass, done, interrupted or failed

A separate small file, rather than a field in ``meta.json``, so every ``meta.json`` watcher is not woken
every two seconds. See docs/OUTPUT_SPEC.md, "heartbeat.json". A meeting's status as a whole (the heartbeat
together with ``meta.json`` and ``DONE``) is worked out in ``status``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from . import meeting as mt

log = logging.getLogger(__name__)

INTERVAL_S = 2.0
STALE_AFTER_S = 15.0  # several missed beats: one slow disk write or a busy reader never trips it

ACTIVE = "active"
RECORDER_DIED = "recorder_died"
NOT_RECORDING = "not_recording"


class Heartbeats:
    """Beats for every meeting folder it holds, on one daemon thread: ``hold`` beats at once and from then
    on every ``interval_s``, until as many ``release`` calls as ``hold`` calls; ``stop`` ends the thread
    (the files keep their last beat). The thread starts with the first ``hold``."""

    def __init__(self, interval_s: float = INTERVAL_S):
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._held: dict[str, tuple[mt.MeetingFolder, int]] = {}  # meeting id -> (folder, hold count)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def hold(self, folder: mt.MeetingFolder) -> None:
        with self._lock:
            _, n = self._held.get(folder.meeting_id, (folder, 0))
            self._held[folder.meeting_id] = (folder, n + 1)
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name="heartbeats", daemon=True)
                self._thread.start()
        _beat(folder)

    def release(self, folder: mt.MeetingFolder) -> None:
        with self._lock:
            _, n = self._held.get(folder.meeting_id, (folder, 0))
            if n <= 1:
                self._held.pop(folder.meeting_id, None)
            else:
                self._held[folder.meeting_id] = (folder, n - 1)

    def stop(self) -> None:
        """Stop beating for all of them. Safe to call twice."""
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            with self._lock:
                folders = [f for f, _ in self._held.values()]
            for folder in folders:
                _beat(folder)


class Heartbeat:
    """Beats for one meeting folder on a daemon thread, from ``start`` until ``stop``."""

    def __init__(self, folder: mt.MeetingFolder, interval_s: float = INTERVAL_S):
        self.folder = folder
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._beat()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop beating; the file keeps the last beat. Safe to call twice."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._beat()

    def _beat(self) -> None:
        _beat(self.folder)

    def __enter__(self) -> "Heartbeat":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def _beat(folder: mt.MeetingFolder) -> None:
    try:
        folder.beat()
    except OSError:
        # e.g. a reader holds the file open on Windows: the next beat tries again, well within
        # STALE_AFTER_S.
        log.debug("Heartbeat write failed for %s", folder.meeting_id, exc_info=True)
    except Exception:  # noqa: BLE001 - a dead thread would report a live process as gone
        log.exception("Heartbeat failed for %s", folder.meeting_id)


def last_beat(folder: mt.MeetingFolder) -> datetime | None:
    """The time of the last beat, or None when there is no readable heartbeat."""
    try:
        with open(folder.heartbeat_path, encoding="utf-8") as fh:
            at = datetime.fromisoformat(json.load(fh)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if at.utcoffset() is not None else None

