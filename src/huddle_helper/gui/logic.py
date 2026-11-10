"""What the Transcript window says, as pure functions (no Tk import; tested without a display).

* ``normalize_ops``     how the view applies a model change list
* ``line_time`` ``gap_text`` ``mark_text`` ``short_name``   row texts
* ``meeting_phase``     recording / stopping / processing / ... for the displayed meeting
* ``header_view`` ``notice_view`` ``status_view``   header, notice bar and status bar contents
* ``format_eta`` ``level_bars``
* ``list_meetings`` ``default_meeting``   the "Open meeting..." picker and which meeting to show
* ``export_text``       the Export dialog's output
* ``mute_bar_view`` ``mute_button_view`` ``bookmark_items`` ``mark_changes`` ``recording_status_text``
  bookmarks and mute (WP5): the amber bar, the header button, the Bookmark menu, the tray tooltip
* ``hotkey_row_status``  what Settings → Hotkeys says next to one combo
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .. import export as export_mod, meeting as mt
from .. import marks as marks_mod
from ..transcript_model import GapRow, MarkRow, Op, Reset, TranscriptModel, fmt_elapsed
from . import theme

# -- ops ---------------------------------------------------------------------


def normalize_ops(ops: list[Op]) -> list[Op] | None:
    """None means "redraw everything from model.rows" (the list contains a Reset: the rows after it
    are already in the model, so replaying them on top of a redraw would double them); otherwise
    the ops to replay in order."""
    return None if any(isinstance(op, Reset) for op in ops) else ops


# -- row texts -----------------------------------------------------------------


def line_time(model: TranscriptModel, ms: int) -> str:
    """Clock time ``HH:MM:SS`` of a meeting time (meeting start + ms); elapsed ``MM:SS`` without a start."""
    start = model.started
    if start is None:
        s = fmt_elapsed(ms)
        return s[3:] if s.startswith("00:") else s
    return (start + timedelta(milliseconds=max(0, int(ms)))).strftime("%H:%M:%S")


def fmt_duration(ms: int) -> str:
    s = max(0, ms) / 1000
    return f"{max(1, round(s))} s" if s < 60 else f"{round(s / 60)} min"


def short_name(name: str, max_chars: int = 18) -> str:
    return name if len(name) <= max_chars else name[:max_chars - 1].rstrip() + "…"


def gap_text(row: GapRow, model: TranscriptModel) -> str:
    span = (f"{line_time(model, row.start_ms)}–{line_time(model, row.end_ms)} "
            f"({fmt_duration(row.end_ms - row.start_ms)})")
    if row.muted:
        return f"— You muted your mic {span} —"
    who = "your mic" if row.source == "mic" else "the call"
    why = f"; {row.reason}" if row.reason else ""
    return f"— No audio from {who} {span}{why} —"


def mark_text(row: MarkRow, model: TranscriptModel) -> str:
    name = model.kind_name(row.kind)
    return f"{name} · {row.label}" if row.label else name


# -- the displayed meeting's phase -----------------------------------------------

RECORDING, STOPPING, PROCESSING, WAITING = "recording", "stopping", "processing", "waiting"
FINAL, RECOVERED, FAILED, UNFINISHED, LIVE_ONLY = "final", "recovered", "failed", "unfinished", "live"


def meeting_phase(meeting_id: str, model: TranscriptModel, state) -> str:
    """``state`` is ``AppState`` (phase, last_meeting, finalize)."""
    if state is not None:
        if state.last_meeting == meeting_id and state.phase == "recording":
            return RECORDING
        if state.last_meeting == meeting_id and state.phase == "stopping":
            return STOPPING
        fin = getattr(state, "finalize", None)
        if fin is not None and fin.meeting_id == meeting_id:
            return PROCESSING
    status = model.status
    if model.final:
        return RECOVERED if (model.interrupted or model.meta.get("clean_exit") is False) else FINAL
    if status == mt.STATUS_FAILED:
        return FAILED
    if status == mt.STATUS_PROCESSING:
        return WAITING
    if status in (mt.STATUS_RECORDING, mt.STATUS_INTERRUPTED):
        return UNFINISHED
    return LIVE_ONLY


def elapsed_text(seconds: float) -> str:
    """``12:41`` or ``1:02:03``."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


@dataclass(frozen=True)
class HeaderView:
    dot: str  # "recording" | "busy" | "idle" | "error"
    text: str


def header_view(meeting_id: str, model: TranscriptModel, state) -> HeaderView:
    phase = meeting_phase(meeting_id, model, state)
    n = model.line_count
    lines = f"{n} line{'' if n == 1 else 's'}"
    if phase == RECORDING:
        elapsed = state.recording.elapsed_s if getattr(state, "recording", None) else 0
        return HeaderView("recording", f"{elapsed_text(elapsed)} · {lines} · live")
    if phase == STOPPING:
        return HeaderView("busy", "Finishing live transcript…")
    if phase == PROCESSING:
        return HeaderView("busy", "Refining speaker labels")
    if phase == WAITING:
        return HeaderView("busy", "Waiting for the final pass")
    if phase == FAILED:
        return HeaderView("error", "Final pass failed")
    if phase == UNFINISHED:
        return HeaderView("idle", f"Not finished · {lines}")
    mins = f"{max(1, round(model.duration_ms / 60000))} min"
    if phase == RECOVERED:
        return HeaderView("idle", f"Recovered after a crash · final · {mins} · {lines}")
    if phase == FINAL:
        return HeaderView("idle", f"Final · {mins} · {lines}")
    return HeaderView("idle", f"{mins} · {lines}")


@dataclass(frozen=True)
class NoticeView:
    text: str
    level: str  # "info" | "error"


def notice_view(meeting_id: str, model: TranscriptModel, state) -> NoticeView | None:
    phase = meeting_phase(meeting_id, model, state)
    if phase == FAILED:
        err = str(model.meta.get("error") or "unknown error")
        return NoticeView(f"The final pass failed ({err}). Showing the live transcript; it can be "
                          "re-run with: huddle-helper finalize <folder>", "error")
    if phase in (RECORDING, STOPPING, PROCESSING, WAITING, LIVE_ONLY, UNFINISHED):
        tail = " after Stop." if phase in (RECORDING, STOPPING) else "."
        text = f"Provisional: speaker labels may change{tail}"
        problems = list(getattr(state, "hotkey_problems", None) or []) if phase == RECORDING else []
        if problems:
            text += "\nHotkeys not working: " + "; ".join(problems) + ". The buttons and the tray menu still work."
        return NoticeView(text, "info")
    if model.conflicts:
        n = len(model.conflicts)
        return NoticeView(f"{n} speaker name conflict{'' if n == 1 else 's'} after the final pass.", "info")
    return None


def format_eta(seconds: float | None) -> str:
    """'about 6 min left'; '' without an estimate."""
    if seconds is None:
        return ""
    minutes = seconds / 60
    if minutes < 1:
        return "less than a minute left"
    minutes = round(minutes / 5) * 5 if minutes >= 20 else max(1, round(minutes))
    if minutes >= 90:
        h, m = divmod(int(minutes), 60)
        return f"about {h} h {m} min left" if m else f"about {h} h left"
    return f"about {int(minutes)} min left"


def level_bars(level: float, width: int = 5) -> str:
    """Meter as filled and empty blocks. Log scale: a peak of 0.01 (-40 dB) is empty, 1.0 is full."""
    if level <= 0.01:
        filled = 0
    else:
        filled = int(round((math.log10(min(level, 1.0)) + 2) / 2 * width))
    filled = max(0, min(width, filled))
    return "▮" * filled + "▯" * (width - filled)


@dataclass(frozen=True)
class StatusView:
    mic: str | None  # level bars, or None when there are no levels to show
    call: str | None
    text: str  # lag/backlog in a recording; stage, percent and time left while processing
    stop_enabled: bool
    show_stop: bool
    mic_muted: bool = False  # ``mic`` then reads "muted" in place of the level meter


def status_view(meeting_id: str, model: TranscriptModel, state) -> StatusView:
    phase = meeting_phase(meeting_id, model, state)
    if phase in (RECORDING, STOPPING):
        rec = getattr(state, "recording", None)
        if rec is None:
            return StatusView(None, None, "", False, True)
        lv = rec.levels or {}
        muted = bool(getattr(rec, "mic_muted", False))
        return StatusView("muted" if muted else level_bars(lv.get("mic", 0.0)), level_bars(lv.get("system", 0.0)),
                          f"Lag {rec.lag_s:.1f} s   Backlog {rec.backlog}", phase == RECORDING, True, muted)
    if phase == PROCESSING:
        fin = state.finalize
        parts = [f"{fin.stage} {fin.frac:.0%}" if fin.frac > 0 else fin.stage]
        if fin.overall > 0:
            parts.append(f"{fin.overall:.0%} overall")
        eta = format_eta(fin.eta_s)
        if eta:
            parts.append(eta)
        return StatusView(None, None, " · ".join(parts), False, False)
    if phase == WAITING:
        ahead = max(0, getattr(state, "finalize_queue", 0) - 1)
        text = (f"Waiting for {ahead} other meeting{'' if ahead == 1 else 's'} to finish" if ahead
                else "Starting the final pass…")
        return StatusView(None, None, text, False, False)
    if phase == FAILED:
        return StatusView(None, None, "Showing the live transcript", False, False)
    return StatusView(None, None, "", False, False)


# -- bookmarks and mute (WP5) ----------------------------------------------------------


def _mic_muted(meeting_id: str, model: TranscriptModel, state) -> bool:
    rec = getattr(state, "recording", None)
    return (meeting_phase(meeting_id, model, state) == RECORDING and rec is not None
            and bool(getattr(rec, "mic_muted", False)))


@dataclass(frozen=True)
class MuteBarView:
    label: str  # "MIC MUTED", bold
    text: str


def _in_room(model: TranscriptModel) -> bool:
    return bool((model.meta.get("settings") or {}).get("diarize_mic"))


def mute_bar_view(meeting_id: str, model: TranscriptModel, state) -> MuteBarView | None:
    """The amber bar under the header, only while the displayed meeting is recording and muted."""
    if not _mic_muted(meeting_id, model, state):
        return None
    text = "You're muted in the recording (the call still hears you)."
    if _in_room(model):
        text += " Everyone in the room is out of the recording too."
    return MuteBarView("MIC MUTED", text)


@dataclass(frozen=True)
class MuteButtonView:
    visible: bool
    text: str
    tooltip: str


def mute_button_view(meeting_id: str, model: TranscriptModel, state, hotkey: str = "") -> MuteButtonView:
    """The header's mute toggle: shown while the displayed meeting is recording (not while stopping)."""
    if meeting_phase(meeting_id, model, state) != RECORDING:
        return MuteButtonView(False, "", "")
    muted = _mic_muted(meeting_id, model, state)
    key = f" [{hotkey}]" if hotkey else ""
    tip = ("Takes your microphone out of this recording only. The call app still hears you."
           + (" With separate speakers on the mic, everyone in the room is muted." if _in_room(model) else "")
           + key)
    return MuteButtonView(True, "Unmute me" if muted else "Mute me in the recording", tip)


@dataclass(frozen=True)
class BookmarkItem:
    kind: str
    label: str  # "⚑ Bookmark"
    hotkey: str  # "Ctrl+Alt+B", "" when off

    @property
    def menu_text(self) -> str:
        return f"{self.label}    {self.hotkey}" if self.hotkey else self.label


def bookmark_items(hotkeys: dict[str, str] | None = None, custom: dict[str, str] | None = None) -> list[BookmarkItem]:
    """The four types, in menu order, with symbol and hotkey (Bookmark split button, tray menu, line menu)."""
    hotkeys = hotkeys or {}
    items = [BookmarkItem(k, f"{theme.KIND_SYMBOLS[k]} {marks_mod.KIND_NAMES[k]}", hotkeys.get(k, ""))
             for k in marks_mod.KINDS]
    items += [BookmarkItem(k, f"{theme.KIND_SYMBOLS['bookmark']} {name}", hotkeys.get(k, ""))
              for k, name in (custom or {}).items()]
    return items


def mark_changes(row: MarkRow, label: str, kind: str) -> dict:
    """Keyword arguments for ``AppController.edit_mark`` that turn ``row`` into (label, kind); {} if the
    same."""
    out = {}
    if label.strip() != row.label:
        out["label"] = label.strip()
    if kind != row.kind:
        out["kind"] = kind
    return out


def bookmark_feedback(result: dict, model_kind_names: dict[str, str] | None = None) -> str:
    """One line for the status bar after a press: what the 2 s rule did."""
    action, kind = result.get("action"), result.get("kind", "")
    name = marks_mod.kind_name(kind, model_kind_names)
    if action == "added":
        return f"{name} added"
    if action == "retyped":
        return f"Changed to {name}"
    if action == "ignored":
        return f"{name} already added"
    return ""


def hotkey_row_status(check, registration=None, live: bool = False, duplicate_of: str | None = None
                      ) -> tuple[str, str]:
    """(text, level) for one row of Settings → Hotkeys; level is "ok", "warn", "error" or "info".

    ``check`` is a ``hotkeys.ComboCheck``; ``registration`` the ``hotkeys.Registration`` for this exact
    text (None when not tried); ``live`` says the registration is the running recording's rather than a
    try-and-release; ``duplicate_of`` names another action with the same combo.
    """
    if check.off:
        return "Off", "info"
    if not check.valid:
        return check.error, "error"
    if duplicate_of:
        return f"Same combination as “{duplicate_of}”. Each action needs its own.", "error"
    if registration is None:
        text, level = "Not checked yet. Click “Check”.", "info"
    elif registration.ok:
        text, level = ("Registered now." if live else "Free: it will be registered while recording."), "ok"
    else:
        text, level = f"Not registered: {registration.error}. Pick another combination.", "error"
    if check.altgr:
        text, level = f"{text} {check.warning}", ("error" if level == "error" else "warn")
    return text, level


def recording_status_text(elapsed_s: float, lines: int, lag_s: float = 0.0, marks: int = 0,
                          muted: bool = False) -> str:
    """The tray tooltip while recording: ``● Recording 12:41 · 84 lines · 3 bookmarks · your mic is muted``."""
    text = f"● Recording {elapsed_text(elapsed_s)} · {lines} lines"
    if lag_s >= 5:
        text += f" · {lag_s:.0f}s behind"
    if marks:
        text += f" · {marks} bookmark{'' if marks == 1 else 's'}"
    if muted:
        text += " · your mic is muted"
    return text


# -- picker -----------------------------------------------------------------------


@dataclass(frozen=True)
class MeetingEntry:
    meeting_id: str
    title: str
    started_at: str
    status: str
    lines: int
    final: bool
    path: Path

    @property
    def label(self) -> str:
        when = self.started_at[:16].replace("T", " ")
        state = "final" if self.final else (self.status or "?")
        return f"{when}   {self.title}   [{state}, {self.lines} lines]"


def list_meetings(meetings_dir: Path, limit: int = 200) -> list[MeetingEntry]:
    """Newest first. Reads each meta.json only (cheap); unreadable folders are skipped."""
    out: list[MeetingEntry] = []
    for folder in mt.iter_meetings(meetings_dir):
        try:
            meta = folder.read_meta()
        except (OSError, ValueError):
            continue
        out.append(MeetingEntry(folder.meeting_id, str(meta.get("title") or folder.meeting_id),
                                str(meta.get("started_at") or ""), str(meta.get("status") or ""),
                                int(meta.get("final_segments") or meta.get("segments") or 0),
                                folder.is_done, folder.path))
    out.sort(key=lambda e: (e.started_at, e.meeting_id), reverse=True)
    return out[:limit]


def default_meeting(meetings_dir: Path, *, recording: mt.MeetingFolder | None = None,
                    finalizing_id: str | None = None, last_id: str | None = None) -> mt.MeetingFolder | None:
    """Which meeting the window shows when asked to open without a choice: the one being recorded,
    else the one being finalized, else the last one recorded in this run, else the newest on disk."""
    if recording is not None:
        return recording
    for mid in (finalizing_id, last_id):
        if mid and (meetings_dir / mid / mt.META).exists():
            return mt.MeetingFolder(meetings_dir / mid)
    found = list_meetings(meetings_dir, 1)
    return mt.MeetingFolder(found[0].path) if found else None


# -- export -------------------------------------------------------------------------


def export_text(model: TranscriptModel, fmt: str, times: str, join: bool, *, clipboard: bool = False) -> str:
    """Text of the Export dialog. For the clipboard Tk converts newlines itself, so no CRLF."""
    return export_mod.render(model, fmt, times, join, crlf=False if clipboard else None)
