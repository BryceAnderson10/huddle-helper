"""Shared test helpers: build fixture meeting folders."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from huddle_helper import meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.segments import Segment
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event

STARTED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)


def _segments(lines) -> list[Segment]:
    """Accept Segment, dict (Segment fields) or (source, start_ms, end_ms, speaker, text) tuples."""
    out = []
    for line in lines:
        if isinstance(line, Segment):
            out.append(line)
        elif isinstance(line, dict):
            out.append(Segment(line["source"], line["start_ms"], line["end_ms"], line["text"],
                               line.get("confidence", 0.9), line.get("speaker", "")))
        else:
            source, start_ms, end_ms, speaker, text = line
            out.append(Segment(source, start_ms, end_ms, text, 0.9, speaker))
    return out


def _write_events(path: Path, meeting_id: str, started_at: str, segments: list[Segment]) -> None:
    end_ms = max((s.end_ms for s in segments), default=0)
    with JsonlWriter(path, mode="w") as w:
        w.write(meeting_start_event(meeting_id, started_at))
        for i, seg in enumerate(segments, 1):
            w.write(seg.to_event(i))
        w.write(meeting_end_event(started_at, len(segments), last_end_ms=end_ms))


def make_meeting(tmp_path: Path, lines, *, final=None, marks=None, tags=None, gaps=None,
                 status: str = mt.STATUS_COMPLETE, title: str = "Fixture",
                 started_at: datetime = STARTED_AT) -> mt.MeetingFolder:
    """Build a valid meeting folder under ``tmp_path`` and return it.

    ``lines`` (live transcript) and ``final`` (final transcript, which also creates DONE) take
    dicts or (source, start_ms, end_ms, speaker, text) tuples. ``marks`` and ``tags`` are lists
    of event dicts for marks.jsonl and speaker_tags.jsonl; ``gaps`` goes into meta.json.
    """
    segs = _segments(lines)
    folder = mt.MeetingFolder.create(tmp_path, title, started_at, {})
    started = mt.iso(started_at)
    _write_events(folder.live_path, folder.meeting_id, started, segs)
    folder.update_meta(status=status, clean_exit=True, ended_at=started, segments=len(segs),
                       gaps=[dict(g) for g in (gaps or [])])
    if final is not None:
        final_segs = _segments(final)
        _write_events(folder.final_path, folder.meeting_id, started, final_segs)
        folder.update_meta(final_written_at=started, final_segments=len(final_segs))
        folder.mark_done()
    for path, events in ((folder.marks_path, marks), (folder.tags_path, tags)):
        for event in events or []:
            mt.append_jsonl(path, event)
    return folder


def play_stub(**over) -> dict:
    """The click-to-hear members of ``AppController`` that the window uses, for SimpleNamespace stubs.
    ``calls`` collects (name, args) of play_clip / stop_playback / set_keep_audio."""
    from huddle_helper.app import PlayResult

    calls: list = []
    stub = {
        "playing_key": None,
        "playing_run": False,
        "recent_oldest_ms": lambda folder: None,
        "play_calls": calls,
        "stop_playback": lambda: calls.append(("stop",)),
        "play_clip": lambda folder, clip, confirmed=False: calls.append(("play", clip.key, confirmed)) or PlayResult("playing"),
        "play_from": lambda folder, clips, confirmed=False: calls.append(("run", [c.key for c in clips], confirmed)) or PlayResult("playing"),
        "set_keep_audio": lambda folder, keep: calls.append(("keep", keep)),
    }
    stub.update(over)
    return stub


CHUNK = SAMPLE_RATE // 10  # 100 ms, like the file capture


class ManualCapture:
    """Stands in for the devices: the test is the capture thread and delivers audio when it says so."""

    realtime = False
    device_names = {"mic": "test", "system": "test"}

    def __init__(self, on_audio, **_):
        self.on_audio = on_audio

    def start(self, t0):
        pass

    def stop(self):
        pass

    def push(self, mic: np.ndarray, system: np.ndarray) -> None:
        n = max(len(mic), len(system))
        mic = np.concatenate([mic, np.zeros(n - len(mic), np.float32)])
        system = np.concatenate([system, np.zeros(n - len(system), np.float32)])
        for i in range(0, n, CHUNK):
            self.on_audio("mic", mic[i:i + CHUNK])
            self.on_audio("system", system[i:i + CHUNK])


def no_wait(_seconds: float) -> None:
    """Recover's re-check of a stale heartbeat, without the real one-beat sleep."""


def crash(folder) -> None:
    """Leave ``folder`` as a crash does: its last heartbeat long stale (status stays as it is)."""
    from datetime import timedelta

    from huddle_helper import meeting as mt

    folder.beat(mt.now_local() - timedelta(minutes=5))
