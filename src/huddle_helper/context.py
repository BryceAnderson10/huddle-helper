"""The read-only context command (#42, ADR 0001): a bounded window of one meeting as JSON.

``huddle-helper context`` prints ``answer(...)``. A downstream app (any language, or wrapped as an AI
tool) runs it instead of writing its own file tailer. The meeting files stay the contract
(docs/OUTPUT_SPEC.md, "Reading a meeting from another app"); nothing here writes to them.

Which meeting
    ``active``   the meeting recording right now with a fresh heartbeat (status ``RECORDING``). With
                 none, an answer with ``meeting: null`` that names the meeting that just ended.
    ``latest``   (default) the active meeting, else the most recently started one in any state, even if
                 it started seconds ago. ``meeting.selected_as`` says which of the two it got.
    an id        that meeting.

Which transcript
    ``auto`` (default) the final file once ``DONE`` exists, else the live file; ``live`` or ``final`` on
    request. ``transcript.kind`` and ``transcript.version`` say which. A final line also carries
    ``live_wording``: what the live file said at that time when the words differ (``live_wording.py``).

Which window (all on the meeting clock; lines are chosen by time, never by ``seq``)
    the last ``DEFAULT_LAST_MS`` (default), ``last_s``, ``from_s``/``to_s``, around a bookmark
    (``BOOKMARK_BEFORE_MS`` before, ``BOOKMARK_AFTER_MS`` after its ``at_ms``), all capped at
    ``MAX_WINDOW_MS``; or ``whole`` (no cap).

Reading reuses ``MeetingSession`` (so the window, the export and this command read a meeting the same
way): ``JsonlTail`` holds back a half-written last line, and line names follow the same rules.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path

from . import attendees as attendees_mod, marks as marks_mod, meeting as mt, status as status_mod
from .library import DeleteRefused, meeting_folder
from .playback import bookmark_range
from .session import MeetingSession
from .live_wording import live_wording
from .transcript_model import GapRow, LineRow, MarkRow, TranscriptModel, _parse_iso, live_overlaps
from .writer import read_events

DEFAULT_LAST_MS = 3 * 60_000
BOOKMARK_BEFORE_MS = 3 * 60_000
BOOKMARK_AFTER_MS = 30_000
MAX_WINDOW_MS = 15 * 60_000

ACTIVE, LATEST = "active", "latest"
AUTO, LIVE, FINAL = "auto", "live", "final"

READ_TRIES, READ_PAUSE_S = 5, 0.05  # meta.json is replaced atomically; on Windows a reader can lose a race

ATTENDEES_MEANING = ("Names typed in the Start dialog to make naming speakers quicker. "
                     "Not a record of who attended, and not who spoke.")
# Where a line's speaker name came from.
FROM_TAGS, FROM_SPEAKERS = mt.TAGS, mt.SPEAKERS


class ContextError(Exception):
    def __init__(self, code: str, message: str, file: Path | None = None):
        super().__init__(message)
        self.code, self.message, self.file = code, message, file

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.file is not None:
            out["file"] = str(self.file)
        return out


# -- reading with retries ------------------------------------------------------------------------------
def _retry(fn, path: Path):
    for attempt in range(READ_TRIES):
        try:
            return fn()
        except (OSError, ValueError) as exc:
            last = exc
            if attempt + 1 < READ_TRIES:
                time.sleep(READ_PAUSE_S)
    raise ContextError("unreadable", f"Could not read {path.name}: {last}", path)


def _read_meta(folder: mt.MeetingFolder) -> dict:
    meta = _retry(folder.read_meta, folder.meta_path)
    if not isinstance(meta, dict):
        raise ContextError("unreadable", "meta.json is not a JSON object", folder.meta_path)
    return meta


def _check_readable(path: Path) -> None:
    """Raise ``ContextError`` when ``path`` exists but cannot be opened (``JsonlTail`` would be silent)."""
    if path.exists():
        def probe():
            with open(path, "rb"):
                pass
        _retry(probe, path)


# -- meeting summary -----------------------------------------------------------------------------------
def _heartbeat(files: status_mod.Files, state: str, now: datetime) -> dict:
    beat = files.last_beat
    return {"state": status_mod.heartbeat_state(state),
            "last_beat": beat.isoformat(timespec="milliseconds") if beat else None,
            "age_s": round((now - beat).total_seconds(), 1) if beat else None}


def _summary(folder: mt.MeetingFolder, meta: dict, files: status_mod.Files, now: datetime) -> dict:
    state = files.status(now)
    return {"id": folder.meeting_id, "title": str(meta.get("title") or folder.meeting_id),
            "state": state, "active": state == status_mod.RECORDING,
            "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"),
            "heartbeat": _heartbeat(files, state, now)}


def _started_key(folder: mt.MeetingFolder, meta: dict) -> tuple:
    started = _parse_iso(meta.get("started_at"))
    ts = started.timestamp() if started is not None and started.tzinfo is not None else float("-inf")
    return (ts, folder.meeting_id)


# -- choosing the meeting ------------------------------------------------------------------------------
MINUTE_PREFIX = len("YYYY-MM-DD_HHMM")  # meeting ids start with their start minute


def _scan(meetings_dir: Path, now: datetime, warnings: list) -> tuple[list[tuple], list[tuple]]:
    """``(rows, unreadable)``: ``(folder, meta, files)`` oldest start first, and
    ``(meeting_id, ContextError)`` for folders whose ``meta.json`` could not be read."""
    rows, unreadable = [], []
    for folder in mt.iter_meetings(meetings_dir):
        try:
            meta = _read_meta(folder)
        except ContextError as exc:
            warnings.append(exc.as_dict())
            unreadable.append((folder.meeting_id, exc))
            continue
        rows.append((folder, meta, status_mod.read(folder, meta)))
    rows.sort(key=lambda r: _started_key(r[0], r[1]))
    return rows, unreadable


def _refuse_if_maybe_newer(chosen_id: str | None, unreadable: list[tuple]) -> None:
    """A meeting we could not read may be newer than the one chosen (or be the active one): an explicit
    error beats quietly answering with an older meeting. Older unreadable meetings are only warnings."""
    for meeting_id, exc in unreadable:
        if chosen_id is None or meeting_id[:MINUTE_PREFIX] >= chosen_id[:MINUTE_PREFIX]:
            raise exc


# -- the window ------------------------------------------------------------------------------------------
def _clock_now_ms(model: TranscriptModel, now: datetime) -> int:
    start = model.started
    if start is None or start.tzinfo is None:
        return model.last_line_end_ms
    return max(0, int((now - start).total_seconds() * 1000))


def _window(model: TranscriptModel, end_ref: int, *, last_s, from_s, to_s, bookmark, whole) -> dict:
    if whole:
        return {"mode": "whole", "start_ms": 0, "end_ms": max(end_ref, model.last_line_end_ms), "capped": False}
    if bookmark is not None:
        mark = next((m for m in model.marks if m.id == bookmark), None)
        if mark is None:
            raise ContextError("bookmark_not_found", f"No bookmark {bookmark} in this meeting")
        start, end = bookmark_range(mark.at_ms, BOOKMARK_BEFORE_MS, BOOKMARK_AFTER_MS)
        return {"mode": "bookmark", "bookmark_id": mark.id, "start_ms": start, "end_ms": end, "capped": False}
    if from_s is not None:
        start, end = int(from_s * 1000), int(to_s * 1000)
        capped = end - start > MAX_WINDOW_MS
        return {"mode": "range", "start_ms": start, "end_ms": start + MAX_WINDOW_MS if capped else end,
                "capped": capped}
    want = DEFAULT_LAST_MS if last_s is None else int(last_s * 1000)
    capped = want > MAX_WINDOW_MS
    want = min(want, MAX_WINDOW_MS)
    return {"mode": "last", "start_ms": max(0, end_ref - want), "end_ms": end_ref, "capped": capped}


def _check_request(meeting, transcript, last_s, from_s, to_s, bookmark, whole) -> None:
    if transcript not in (AUTO, LIVE, FINAL):
        raise ContextError("bad_request", f"transcript must be auto, live or final, not {transcript!r}")
    if not meeting:
        raise ContextError("bad_request", "meeting must be active, latest or a meeting id")
    chosen = [last_s is not None, from_s is not None or to_s is not None, bookmark is not None, bool(whole)]
    if sum(chosen) > 1:
        raise ContextError("bad_request", "Give at most one of: last, from/to, bookmark, whole")
    if (from_s is None) != (to_s is None):
        raise ContextError("bad_request", "from and to go together")
    if from_s is not None and not 0 <= from_s < to_s:
        raise ContextError("bad_request", "from must be at least 0 and before to")
    if any(v is not None and not math.isfinite(v) for v in (last_s, from_s, to_s)):
        raise ContextError("bad_request", "Times must be finite numbers of seconds")
    if last_s is not None and last_s <= 0:
        raise ContextError("bad_request", "last must be more than 0 seconds")


# -- the answer ----------------------------------------------------------------------------------------------
def answer(meetings_dir: Path | str, meeting: str = LATEST, transcript: str = AUTO, *,
           last_s: float | None = None, from_s: float | None = None, to_s: float | None = None,
           bookmark: int | None = None, whole: bool = False, now: datetime | None = None) -> dict:
    """The JSON-ready answer. Never raises for a missing or unreadable file: ``ok`` is False and
    ``error`` says why."""
    now = now or mt.now_local()
    out: dict = {"ok": True, "generated_at": now.isoformat(timespec="milliseconds")}
    warnings: list[dict] = []
    try:
        _check_request(meeting, transcript, last_s, from_s, to_s, bookmark, whole)
        picked = _pick(Path(meetings_dir), meeting, now, warnings)
        if isinstance(picked, list):  # no active meeting: name the one that just ended
            out.update(meeting=None, no_active_meeting=True,
                       just_ended=_summary(*picked[-1], now) if picked else None)
        else:
            out.update(_meeting_answer(*picked, transcript, now, warnings, last_s=last_s, from_s=from_s,
                                       to_s=to_s, bookmark=bookmark, whole=whole))
    except ContextError as exc:
        out = {"ok": False, "generated_at": out["generated_at"], "error": exc.as_dict()}
    out["warnings"] = warnings
    return out


def _pick(meetings_dir: Path, meeting: str, now: datetime, warnings: list):
    """``(folder, meta, files, selected_as)``; for ``active`` with none recording, the list of
    ``(folder, meta, files)`` of every meeting, oldest start first."""
    if meeting in (ACTIVE, LATEST):
        rows, unreadable = _scan(meetings_dir, now, warnings)
        active = [r for r in rows if r[2].status(now) == status_mod.RECORDING]
        if active:
            return (*active[-1], ACTIVE)
        _refuse_if_maybe_newer(rows[-1][0].meeting_id if rows else None, unreadable)
        if meeting == ACTIVE:
            return rows
        if not rows:
            raise ContextError("no_meetings", f"No meetings in {meetings_dir}")
        return (*rows[-1], LATEST)
    try:
        folder = meeting_folder(meetings_dir, meeting)
    except DeleteRefused:
        raise ContextError("not_found", f"No meeting {meeting!r} in {meetings_dir}") from None
    meta = _read_meta(folder)
    return folder, meta, status_mod.read(folder, meta), "id"


def _meeting_answer(folder: mt.MeetingFolder, meta: dict, files: status_mod.Files, selected_as: str, transcript: str,
                    now: datetime, warnings: list, **window_req) -> dict:
    if transcript == FINAL and not folder.final_path.exists():
        raise ContextError("final_not_ready", "This meeting has no final transcript yet", folder.final_path)
    use_final = {AUTO: None, LIVE: False, FINAL: True}[transcript]
    if use_final is None:
        use_final = folder.is_done and folder.final_path.exists()
    _check_readable(folder.final_path if use_final else folder.live_path)
    for path in (folder.marks_path, folder.tags_path, folder.speakers_path):
        try:
            _check_readable(path)
        except ContextError as exc:
            warnings.append(exc.as_dict())

    session = MeetingSession(folder, final=use_final)
    model = session.model
    if not model.meta:  # the session's own read lost a race with a rewrite: use the one we have
        model.set_meta(meta)
    else:
        meta = model.meta  # the session's read is the newer one
    session.poll(None)

    state = files.status(now)
    active = state == status_mod.RECORDING
    end_ref = _clock_now_ms(model, now) if active else max(model.duration_ms, model.last_line_end_ms)
    window = _window(model, end_ref, **window_req)
    start, end = window["start_ms"], window["end_ms"]

    lines = [r for r in model.lines if r.end_ms > start and r.start_ms < end]
    gaps = [_gap(g) for g in model.gaps if g.end_ms > start and g.start_ms < end]
    since = meta.get("mic_muted_since_ms")
    if isinstance(since, int) and since < end:
        gaps.append({"stream": "mic", "start_ms": since, "end_ms": None, "reason": mt.MUTED,
                     "deliberate": True, "open": True})
    reasons = []
    if any(not g["deliberate"] for g in gaps):
        reasons.append("lost_audio")
    if active and end > model.last_line_end_ms:
        reasons.append("still_recording")
    if state == status_mod.RECORDER_DIED and end > model.last_line_end_ms:
        reasons.append("recorder_died")
    labels = {r.label for r in lines}

    summary = _summary(folder, meta, files, now)
    summary["selected_as"] = selected_as
    all_lines = model.lines
    return {
        "meeting": summary,
        "transcript": {
            "kind": FINAL if model.final else LIVE,
            "version": model.transcript_version if model.final else None,
            "lines": len(all_lines),
            "start_ms": min((r.start_ms for r in all_lines), default=None),
            "reaches_ms": model.last_line_end_ms if all_lines else None,
            "finished": model.ended_at is not None or model.interrupted,
        },
        "window": {**window, "incomplete": bool(reasons), "incomplete_reasons": reasons},
        "lines": [_line(r, model.final) for r in lines] if not model.final
        else _with_live_wording(folder, lines, warnings),
        "speakers": {
            "conflicts": [c for c in model.conflicts if c.get("speaker") in labels],
            "names_changed_at": _names_changed_at(folder) if model.final else None,
        },
        # the same half-open window as the lines ([start, end)); the whole meeting takes every bookmark
        "bookmarks": [_mark(m, model) for m in model.marks
                      if window["mode"] == "whole" or start <= m.at_ms < end],
        "bookmarks_changed_at": _marks_changed_at(folder),
        "gaps": gaps,
        "attendees": {"names": attendees_mod.from_meta(meta), "meaning": ATTENDEES_MEANING},
    }


def _line(r: LineRow, final: bool) -> dict:
    return {"stream": r.source, "start_ms": r.start_ms, "end_ms": r.end_ms, "text": r.text,
            "confidence": r.confidence, "speaker_label": r.label, "speaker_name": r.name,
            "name_from": (FROM_SPEAKERS if final else FROM_TAGS) if r.name else None}


def _with_live_wording(folder: mt.MeetingFolder, lines: list[LineRow], warnings: list) -> list[dict]:
    """Final lines, each with ``live_wording``: the live text overlapping it in time on its stream when
    the words differ (#44), else ``None``. The live file is only read."""
    live = TranscriptModel(folder)  # only its lines are used: no names, marks or meta needed
    if folder.live_path.exists():
        try:
            live.apply_transcript(_retry(lambda: read_events(folder.live_path), folder.live_path))
        except ContextError as exc:
            warnings.append(exc.as_dict())
    overlaps = live_overlaps(live.lines, lines)
    return [{**_line(r, True), "live_wording": live_wording(r, overlaps.get(r.key, []))} for r in lines]


def _mark(m: MarkRow, model: TranscriptModel) -> dict:
    return {"id": m.id, "type": m.kind, "type_name": model.kind_name(m.kind), "label": m.label,
            "at_ms": m.at_ms}


def _gap(g: GapRow) -> dict:
    return {"stream": g.source, "start_ms": g.start_ms, "end_ms": g.end_ms, "reason": g.reason,
            "deliberate": g.muted}


def _names_changed_at(folder: mt.MeetingFolder) -> str | None:
    try:
        data = mt.read_json(folder.speakers_path)
    except (OSError, ValueError):
        return None
    value = data.get("names_changed_at") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _marks_changed_at(folder: mt.MeetingFolder) -> str | None:
    try:
        return marks_mod.last_changed(read_events(folder.marks_path))
    except OSError:
        return None
