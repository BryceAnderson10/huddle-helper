"""Crash recovery and optional audio auto-purge (F14)."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from . import meeting as mt
from .writer import dumps, meeting_end_event, read_events

log = logging.getLogger(__name__)


def _truncate_partial_line(path: Path) -> None:
    """Drop a half-written last line so appended lines stay valid JSONL."""
    with open(path, "rb+") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size == 0:
            return
        fh.seek(size - 1)
        if fh.read(1) == b"\n":
            return
        fh.seek(0)
        data = fh.read()
        cut = data.rfind(b"\n") + 1
        fh.truncate(cut)


def close_interrupted_live_file(folder: mt.MeetingFolder) -> int:
    """Make the live file end with a meeting_end event; return its segment count."""
    path = folder.live_path
    if not path.exists():
        start = {"type": "meeting_start", "meeting_id": folder.meeting_id,
                 "started_at": folder.read_meta().get("started_at"), "schema": 1}
        path.write_text(dumps(start) + "\n", encoding="utf-8")
    _truncate_partial_line(path)
    events = read_events(path)
    segments = sum(1 for e in events if e.get("type") == "segment")
    if not events or events[-1].get("type") != "meeting_end":
        last_end = max((e.get("end_ms", 0) for e in events if e.get("type") == "segment"), default=0)
        meta = folder.read_meta()
        ended_at = meta.get("ended_at") or mt.iso(mt.datetime.fromtimestamp(path.stat().st_mtime).astimezone())
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(meeting_end_event(ended_at, segments, interrupted=True, last_end_ms=last_end)) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return segments


def find_unfinished(meetings_dir: Path) -> list[mt.MeetingFolder]:
    out = []
    for folder in mt.iter_meetings(meetings_dir):
        if folder.is_done:
            continue
        try:
            status = folder.read_meta().get("status")
        except (OSError, ValueError):
            continue
        if status in (mt.STATUS_RECORDING, mt.STATUS_PROCESSING, mt.STATUS_INTERRUPTED):
            out.append(folder)
    return out


def recover(meetings_dir: Path, finalize: Callable[[mt.MeetingFolder], None] | None = None,
            skip: set[str] | None = None) -> list[str]:
    """Close out meetings left unfinished by a crash, then finalize them.

    ``skip`` holds meeting ids that are live in this process and must be left alone.
    """
    recovered = []
    for folder in find_unfinished(meetings_dir):
        if skip and folder.meeting_id in skip:
            continue
        meta = folder.read_meta()
        try:
            if meta.get("status") == mt.STATUS_RECORDING or meta.get("clean_exit") is not True:
                count = close_interrupted_live_file(folder)
                folder.update_meta(status=mt.STATUS_INTERRUPTED, clean_exit=False, segments=count,
                                   ended_at=meta.get("ended_at") or read_events(folder.live_path)[-1].get("ended_at"))
                log.warning("Meeting %s did not finish cleanly; recovered %d segments", folder.meeting_id, count)
            if finalize is not None:
                finalize(folder)
            recovered.append(folder.meeting_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not recover meeting %s", folder.meeting_id)
            try:
                folder.update_meta(status=mt.STATUS_FAILED, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
    return recovered


def purge_old_audio(meetings_dir: Path, days: int, now: float | None = None) -> list[str]:
    """Delete kept audio for finished meetings older than ``days``. 0 disables."""
    if days <= 0:
        return []
    now = now or time.time()
    cutoff = now - days * 86400
    purged = []
    for folder in mt.iter_meetings(meetings_dir):
        if not folder.is_done or not folder.audio_dir.exists():
            continue
        if folder.done_path.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(folder.audio_dir, ignore_errors=True)
        try:
            folder.update_meta(audio_kept=False, audio_purged_at=mt.iso(mt.now_local()))
        except (OSError, ValueError):
            pass
        purged.append(folder.meeting_id)
        log.info("Purged kept audio for %s", folder.meeting_id)
    return purged
