"""Crash recovery and optional audio auto-purge (F14)."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from . import marks as marks_mod, meeting as mt
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


def _count_frames(path: Path) -> int:
    """Frames that can be read from a FLAC file that was never closed. Its header has no length
    (soundfile reports 2**63-1), and libsndfile raises when a read runs into the unfinished end, so
    read in blocks and, after an error, carry on from the last good position with smaller blocks."""
    import soundfile as sf

    n = 0
    for block in (1 << 16, 1 << 10, 64):
        try:
            with sf.SoundFile(str(path)) as fh:
                fh.seek(n)
                while True:
                    got = len(fh.read(block, dtype="float32"))
                    if not got:
                        return n
                    n += got
        except sf.LibsndfileError:
            continue
    return n


def audio_length_ms(folder: mt.MeetingFolder) -> int | None:
    """How much audio the buffers hold (the longer of mic and system), None without audio files."""
    import soundfile as sf

    best = None
    for source in ("mic", "system"):
        path = folder.audio_file(source)
        if not path.exists():
            continue
        try:
            with sf.SoundFile(str(path)) as fh:
                frames, rate = fh.frames, fh.samplerate
            if frames <= 0 or frames >= 1 << 40:  # a header never finalised
                frames = _count_frames(path)
        except Exception:  # noqa: BLE001
            log.exception("Could not measure %s", path)
            continue
        ms = frames * 1000 // max(rate, 1)
        best = ms if best is None else max(best, ms)
    return best


def close_open_mute(folder: mt.MeetingFolder, meta: dict, last_end_ms: int = 0) -> bool:
    """A crash while muted left ``mic_muted_since_ms`` set: close the period as a ``muted`` gap that
    ends at the last audio (the buffers' length; else the end of the last transcript line), and clear it."""
    since = meta.get("mic_muted_since_ms")
    if since is None:
        return False
    end = audio_length_ms(folder)
    if end is None:
        end = last_end_ms
    folder.record_mute([(int(since), int(end))] if end > since else [], None)
    log.warning("Meeting %s was muted when it stopped; closed the muted period at %d ms", folder.meeting_id, end)
    return True


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
                last_end = max((e.get("end_ms", 0) for e in read_events(folder.live_path)
                                if e.get("type") == "segment"), default=0)
                close_open_mute(folder, meta, last_end)
                folder.update_meta(**marks_mod.summarize(folder))
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
