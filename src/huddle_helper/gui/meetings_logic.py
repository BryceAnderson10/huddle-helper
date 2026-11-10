"""What the Meetings view shows and which buttons work, as pure functions (no Tk import).

* ``meeting_rows``     the list: one ``Row`` per meeting that passes the filter and search
* ``summary_text``     "12 meetings · 2.1 GB · 1.9 GB of it kept audio"
* ``actions_for``      which buttons are enabled for the selection, and the line explaining why not
* ``detail_text``      the line under the list (a failed final pass's error, a queued retry)
* ``confirm_items``    the selection as ``confirm.Item`` for the delete confirmation
* ``delete_message`` ``retry_message``   what the view says after an action
* ``load_model``       a whole meeting as a ``TranscriptModel`` (Export… from the list)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .. import library, meeting as mt
from ..library import LiveState, MeetingInfo, StatusView
from ..session import MeetingSession
from ..transcript_model import TranscriptModel
from . import confirm
from .logic import elapsed_text

COLUMNS = ("Date", "Title", "Length", "Status", "Speakers", "Marks", "Audio")
DASH = "–"


@dataclass(frozen=True)
class Row:
    id: str
    values: tuple[str, ...]  # one per COLUMNS
    kind: str  # library status kind: drives the row colour


def date_text(started_at: str) -> str:
    when = library.parse_time(started_at)
    return when.strftime("%m-%d %H:%M") if when else DASH


def audio_text(info: MeetingInfo) -> str:
    if not info.has_audio:
        return DASH
    return confirm.format_size(info.audio_bytes) if info.audio_bytes is not None else "…"


def row_for(info: MeetingInfo, status: StatusView) -> Row:
    return Row(info.id, (
        date_text(info.started_at), info.title,
        elapsed_text(info.length_s) if info.length_s else DASH,
        status.text,
        str(info.speakers) if info.speakers is not None else DASH,
        str(info.marks),
        audio_text(info)), status.kind)


def meeting_rows(infos: list[MeetingInfo], live: LiveState | None = None, *, status_filter: str = "All",
                 search: str = "") -> list[Row]:
    rows = []
    for info in infos:
        status = library.status_of(info, live)
        if library.matches(info, status, status_filter=status_filter, search=search):
            rows.append(row_for(info, status))
    return rows


def summary_text(infos: list[MeetingInfo], shown: int | None = None) -> str:
    t = library.totals(infos)
    n = f"{t.meetings} meeting{'' if t.meetings == 1 else 's'}"
    if shown is not None and shown != t.meetings:
        n = f"{shown} of {n}"
    if not t.meetings:
        return "No meetings yet."
    if not t.measured:
        return f"{n} · measuring sizes…"
    return f"{n} · {confirm.format_size(t.total_bytes)} · {confirm.format_size(t.audio_bytes)} of it kept audio"


@dataclass(frozen=True)
class Actions:
    open: bool = False
    export: bool = False
    explorer: bool = False
    retry: bool = False
    delete_audio: bool = False
    delete_meeting: bool = False
    why: str = ""  # why the delete buttons are off, when they are


def actions_for(selected: list[MeetingInfo], live: LiveState | None = None) -> Actions:
    if not selected:
        return Actions()
    statuses = [library.status_of(i, live) for i in selected]
    one = len(selected) == 1
    busy = [i for i, s in zip(selected, statuses) if s.busy]
    can_delete = not busy
    why = ""
    if busy:
        why = ("Not available while the meeting is recording or being processed." if len(selected) == 1 else
               "Some of these meetings are recording or being processed; deleting is not available for them.")
    return Actions(
        open=one, export=one, explorer=one,
        retry=any(s.kind == library.FAILED for s in statuses),
        delete_audio=can_delete and any(i.has_audio for i in selected),
        delete_meeting=can_delete, why=why)


def detail_text(selected: list[MeetingInfo], live: LiveState | None = None) -> tuple[str, str]:
    """(text, level) under the list; level is "error", "info" or "" for nothing to say."""
    if len(selected) != 1:
        return "", ""
    info = selected[0]
    status = library.status_of(info, live)
    if status.kind == library.RETRY_WAITING:
        return "Retry queued until recording stops. The final pass competes with live transcription for the CPU.", "info"
    if status.kind == library.FAILED:
        err = info.error or "unknown error"
        tail = "" if info.has_audio else " The audio is gone, so a retry can only use the live transcript."
        return f"The final pass failed: {err}{tail}", "error"
    return "", ""


def confirm_items(infos: list[MeetingInfo], *, audio_only: bool, now: datetime | None = None) -> list[confirm.Item]:
    out = []
    for info in infos:
        if audio_only and not info.has_audio:
            continue
        out.append(confirm.Item(
            info.title, info.audio_bytes if audio_only else info.total_bytes,
            library.hours_since(info.finished_at, now) if info.done else None,
            final_failed=info.status == mt.STATUS_FAILED, has_final=info.done))
    return out


def delete_message(result: library.DeleteResult, *, audio_only: bool) -> tuple[str, str]:
    """(text, level) for the status line after a delete; level "error" when anything went wrong."""
    what = "audio" if audio_only else "meeting"
    n = len(result.done)
    parts = []
    if n:
        if result.how == "deleted":
            parts.append(f"Permanently deleted the {what} of {n} meeting{'' if n == 1 else 's'}." if audio_only
                         else f"Permanently deleted {n} meeting{'' if n == 1 else 's'}.")
        else:
            parts.append(f"Moved the audio of {n} meeting{'' if n == 1 else 's'} to the Recycle Bin." if audio_only
                         else f"Moved {n} meeting{'' if n == 1 else 's'} to the Recycle Bin.")
    if result.refused:
        parts.append(f"{len(result.refused)} not deleted (recording or being processed).")
    if result.failed:
        parts.append(f"{len(result.failed)} could not be deleted: {result.failed[0][1]}")
    if not parts:
        parts.append("Nothing to delete.")
    return " ".join(parts), "error" if (result.refused or result.failed) else "info"


def retry_message(outcomes: list[str]) -> tuple[str, str]:
    started, queued = outcomes.count("started"), outcomes.count("queued")
    refused = outcomes.count("refused")
    parts = []
    if started:
        parts.append(f"Final pass started for {started} meeting{'' if started == 1 else 's'}.")
    if queued:
        parts.append(f"Retry queued until recording stops ({queued}).")
    if refused:
        parts.append(f"{refused} could not be retried (busy or already finished).")
    return " ".join(parts) or "Nothing to retry.", "error" if refused else "info"


def load_model(folder: mt.MeetingFolder) -> TranscriptModel:
    """The whole meeting as the Transcript window would show it (final file once ``DONE`` exists)."""
    session = MeetingSession(folder)
    for _ in range(10_000):
        session.poll(max_events=100_000)
        if not session.has_more:
            break
    session.poll_slow()
    return session.model
