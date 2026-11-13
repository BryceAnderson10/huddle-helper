"""A meeting's status, worked out in one place (#62).

From the files
    ``read(folder)`` takes what the meeting's files say (``meta.json`` status and ``clean_exit``, the
    ``heartbeat.json`` beat, ``DONE``) as a ``Files``; ``Files.status(now)`` gives the status in the
    output spec's vocabulary (docs/OUTPUT_SPEC.md, "heartbeat.json" and "meeting.state"):

    ``RECORDING``      status ``recording`` and a fresh heartbeat
    ``RECORDER_DIED``  status ``recording`` but the heartbeat is stale, missing or unreadable
    ``PROCESSING`` ``COMPLETE`` ``INTERRUPTED`` ``FAILED``   the ``meta.json`` status as it is
                       (an unknown status is passed on as it is, a missing one as ``UNKNOWN``)

    ``Files.recovered``: the recording did not end with Stop, i.e. status ``interrupted`` or
    ``clean_exit`` false (GLOSSARY "Recovered"); ``Files.done``: ``DONE`` exists (finished, safe to read).

With the app's in-process state
    ``shown(files, meeting_id, live, now)`` lays what this app is doing over the file status, by one
    precedence rule: recording here, stopping, its final pass running, a retry waiting for the recording
    to stop, queued for the final pass, finished (``DONE``), then the file status. The app's state wins
    because ``meta.json`` lags it by a moment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import heartbeat as hb, meeting as mt

# From the files (the output spec's vocabulary)
RECORDING = mt.STATUS_RECORDING
RECORDER_DIED = "recorder_died"
PROCESSING = mt.STATUS_PROCESSING
COMPLETE = mt.STATUS_COMPLETE
INTERRUPTED = mt.STATUS_INTERRUPTED
FAILED = mt.STATUS_FAILED
UNKNOWN = "unknown"
# What this app is doing with a meeting (``shown``); RECORDING and PROCESSING are also used for it
STOPPING, QUEUED, RETRY_WAITING = "stopping", "queued", "retry-waiting"


def recovered(meta: dict) -> bool:
    """The recording did not end with Stop: status ``interrupted``, or ``clean_exit`` false."""
    return meta.get("status") == mt.STATUS_INTERRUPTED or meta.get("clean_exit") is False


@dataclass(frozen=True)
class Files:
    """What a meeting's files say about its status, read once (``read``); ``now`` is given when asking."""
    meta_status: str  # meta.json "status", "" when missing
    clean_exit: bool | None
    done: bool  # DONE exists
    last_beat: datetime | None  # heartbeat.json "at", None when missing or unreadable

    def heartbeat_fresh(self, now: datetime | None = None, stale_after_s: float = hb.STALE_AFTER_S) -> bool:
        """The last beat is at most ``stale_after_s`` from ``now``, whatever the status. A beat from the
        future counts as stale: a clock set back after a crash must not keep a dead meeting looking alive."""
        if self.last_beat is None:
            return False
        return abs(((now or mt.now_local()) - self.last_beat).total_seconds()) <= stale_after_s

    def status(self, now: datetime | None = None, stale_after_s: float = hb.STALE_AFTER_S) -> str:
        """``RECORDING`` or ``RECORDER_DIED`` for status ``recording``, by the heartbeat; else the
        ``meta.json`` status as it is (``UNKNOWN`` when missing). ``DONE`` does not change it."""
        if self.meta_status == RECORDING:
            return RECORDING if self.heartbeat_fresh(now, stale_after_s) else RECORDER_DIED
        return self.meta_status or UNKNOWN

    @property
    def recovered(self) -> bool:
        return self.meta_status == INTERRUPTED or self.clean_exit is False


def heartbeat_state(file_status: str) -> str:
    """The heartbeat's reading of a file status, as ``heartbeat.json`` readers name it (``hb.ACTIVE``,
    ``hb.RECORDER_DIED`` or ``hb.NOT_RECORDING``; the context command's ``heartbeat.state``)."""
    return {RECORDING: hb.ACTIVE, RECORDER_DIED: hb.RECORDER_DIED}.get(file_status, hb.NOT_RECORDING)


def read(folder: mt.MeetingFolder, meta: dict | None = None) -> Files:
    """What ``folder``'s files say. Pass ``meta`` when it has just been read; otherwise ``meta.json`` is
    read here, and OSError or ValueError means it cannot be read right now (it is being replaced)."""
    if meta is None:
        meta = folder.read_meta()
        if not isinstance(meta, dict):
            raise ValueError("meta.json is not a JSON object")
    return from_meta(meta, done=folder.is_done, last_beat=hb.last_beat(folder))


def from_meta(meta: dict, *, done: bool, last_beat: datetime | None) -> Files:
    """``Files`` from a ``meta.json`` already read, with ``done`` and the beat as the caller sees them (the
    Transcript window: done once its final transcript is loaded)."""
    clean_exit = meta.get("clean_exit")
    return Files(meta_status=str(meta.get("status") or ""),
                 clean_exit=clean_exit if isinstance(clean_exit, bool) else None,
                 done=done, last_beat=last_beat)


@dataclass(frozen=True)
class Shown:
    """A meeting's status as the app shows it (``shown``)."""
    status: str  # this app's: RECORDING, STOPPING, PROCESSING, RETRY_WAITING, QUEUED; else COMPLETE once
    #              DONE exists, else the file status
    here: bool  # this app is recording, stopping, processing or queueing it (another process may hold
    #             a meeting too: ``Files.heartbeat_fresh``, ADR 0002)
    recovered: bool  # see ``recovered``


def shown(files: Files, meeting_id: str, live=None, now: datetime | None = None) -> Shown:
    """The file status with what this app is doing laid over it. ``live`` is a ``library.LiveState``
    (``AppController.live_state()``; read by its fields, as library imports this module), or None
    outside the app. One precedence rule: recording here,
    stopping, its final pass running, a retry waiting for the recording to stop, queued for the final
    pass, finished (``DONE``), then the file status. The app's state wins because ``meta.json`` lags it."""
    mine = _this_app(meeting_id, live)
    if mine is not None:
        return Shown(mine, True, files.recovered)
    return Shown(COMPLETE if files.done else files.status(now), False, files.recovered)


def _this_app(meeting_id: str, live) -> str | None:
    if live is None:
        return None
    if meeting_id == live.recording:
        return STOPPING if live.stopping else RECORDING
    if meeting_id == live.finalizing:
        return PROCESSING
    if meeting_id in live.waiting:
        return RETRY_WAITING
    if meeting_id in live.queued:
        return QUEUED
    return None
