"""Recorder heartbeat (#40): tells an active meeting from one whose recorder died.

While a meeting records, a thread of its own rewrites ``heartbeat.json`` in the meeting folder every
``INTERVAL_S`` seconds, whatever the audio does: in total silence and while the mic is muted it beats
just the same. It stops when Stop has set ``meta.json`` to ``processing``; a crash stops it too, and
``meta.json`` then stays at ``recording`` until the app next starts. So a reader asks ``liveness``:

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
        try:
            self.folder.beat()
        except OSError:
            # e.g. a reader holds the file open on Windows: the next beat tries again, well within
            # STALE_AFTER_S.
            log.debug("Heartbeat write failed for %s", self.folder.meeting_id, exc_info=True)
        except Exception:  # noqa: BLE001 - a dead thread would report a live recorder as dead
            log.exception("Heartbeat failed for %s", self.folder.meeting_id)


def last_beat(folder: mt.MeetingFolder) -> datetime | None:
    """The time of the last beat, or None when there is no readable heartbeat."""
    try:
        with open(folder.heartbeat_path, encoding="utf-8") as fh:
            at = datetime.fromisoformat(json.load(fh)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if at.utcoffset() is not None else None


def liveness(folder: mt.MeetingFolder, now: datetime | None = None,
             stale_after_s: float = STALE_AFTER_S) -> str:
    """``ACTIVE``, ``RECORDER_DIED`` or ``NOT_RECORDING`` (see the module docstring), from
    ``status.read``. Raises OSError or ValueError when ``meta.json`` cannot be read right now (it is being
    replaced): ask again. Only recover still asks this way; the rest of the app reads ``status``."""
    from . import status  # status reads the heartbeat through this module

    return status.heartbeat_state(status.read(folder).status(now, stale_after_s))
