"""Shared test helpers: build fixture meeting folders."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from huddle_helper import meeting as mt
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
