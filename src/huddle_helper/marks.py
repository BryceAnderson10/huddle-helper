"""Bookmarks: ``marks.jsonl`` events, the reducer, and ``MarkLog`` (LIVE_VIEW "Bookmarks"). No Tk.

Events (append-only, one JSON object per line; see docs/OUTPUT_SPEC.md)

    {"type":"mark","id":1,"kind":"decision","at_ms":381200,"at":"2026-11-09T11:06:24-06:00","via":"hotkey"}
    {"type":"mark_label","id":1,"label":"Ship Friday"}      "" clears the label
    {"type":"mark_kind","id":1,"kind":"action"}
    {"type":"mark_delete","id":1}

``MarkLog`` adds ``"changed_at"`` (when it wrote the line, strictly increasing) to every event; the last
line's is when the bookmarks last changed (``last_changed``). It is not part of what the builders return.

``reduce(events)`` applies them in order and returns ``{id: Mark}``. The transcript model feeds the
same reducer (``transcript_model.reduce_marks`` is this function), so the window, the export and
``MarkLog`` cannot disagree. A kind the reader does not know reads as ``bookmark``
(``Mark.read_kind``); the raw kind is kept so a later ``meta.json`` ``mark_kinds`` can name it.

``MarkLog(folder)`` is the one writer. Ids come from the file under the folder lock (a deleted id is
never reused), so two ``MarkLog`` objects for one folder (the recorder's and a short-lived one for a
past meeting) cannot hand out the same id. The 2 s rule applies to live presses only:

    same kind again within 2 s of the previous live bookmark   -> ignored
    a different kind within 2 s                                -> ``mark_kind`` on the previous bookmark
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import meeting as mt
from .writer import read_events

log = logging.getLogger(__name__)

BOOKMARK = "bookmark"
KIND_NAMES = {"bookmark": "Bookmark", "decision": "Decision", "action": "Action item", "question": "Question"}
KIND_PLURALS = {"bookmark": "Bookmarks", "decision": "Decisions", "action": "Action items",
                "question": "Questions"}
KIND_ORDER = ("decision", "action", "question", "bookmark")  # export grouping order; custom kinds follow
KINDS = tuple(KIND_NAMES)  # the four built-in types, in menu order
KIND_PATTERN = re.compile(r"^[a-z0-9-]+$")  # a user-defined type id (WP9)
VIAS = ("hotkey", "button", "tray", "line")
DOUBLE_PRESS_S = 2.0

MARK, LABEL, KIND, DELETE = "mark", "mark_label", "mark_kind", "mark_delete"


# -- events ----------------------------------------------------------------
def mark_event(mark_id: int, kind: str, at_ms: int, at: str, via: str, seq: int | None = None) -> dict:
    event = {"type": MARK, "id": int(mark_id), "kind": kind, "at_ms": int(at_ms), "at": at, "via": via}
    if seq is not None:
        event["seq"] = int(seq)
    return event


def label_event(mark_id: int, label: str) -> dict:
    return {"type": LABEL, "id": int(mark_id), "label": label}


def kind_event(mark_id: int, kind: str) -> dict:
    return {"type": KIND, "id": int(mark_id), "kind": kind}


def delete_event(mark_id: int) -> dict:
    return {"type": DELETE, "id": int(mark_id)}


# -- reducer -----------------------------------------------------------------
@dataclass
class Mark:
    id: int
    kind: str  # as written; use read_kind() for what to show
    label: str
    at_ms: int
    at: str = ""
    via: str = ""
    seq: int | None = None

    def read_kind(self, extra: Iterable[str] = ()) -> str:
        """The kind to show: built-in, or one of ``extra`` (meta.json ``mark_kinds``), else ``bookmark``."""
        return self.kind if (self.kind in KIND_NAMES or self.kind in extra) else BOOKMARK


def reduce(events: Iterable[dict], state: dict[int, Mark] | None = None) -> dict[int, Mark]:
    """Apply marks.jsonl events in order to ``state`` and return it. Events for an id that does not
    exist (never created, or deleted) are ignored, and so are malformed ones."""
    state = {} if state is None else state
    for e in events:
        kind = e.get("type")
        try:
            mid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        if kind == MARK:
            try:
                at_ms = int(e["at_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            seq = e.get("seq")
            state[mid] = Mark(mid, str(e.get("kind") or BOOKMARK), str(e.get("label") or ""), at_ms,
                              str(e.get("at") or ""), str(e.get("via") or ""),
                              int(seq) if isinstance(seq, int) else None)
        elif mid in state:
            if kind == LABEL:
                state[mid].label = str(e.get("label") or "")
            elif kind == KIND:
                state[mid].kind = str(e.get("kind") or BOOKMARK)
            elif kind == DELETE:
                del state[mid]
    return state


def last_changed(events: Iterable[dict]) -> str | None:
    """When the bookmarks last changed: the ``changed_at`` of the last event that has one (None for a
    file written before the field existed, or no file)."""
    last = None
    for e in events:
        if isinstance(e.get("changed_at"), str):
            last = e["changed_at"]
    return last


def kind_name(kind: str, custom: dict[str, str] | None = None) -> str:
    return KIND_NAMES.get(kind) or (custom or {}).get(kind) or KIND_NAMES[BOOKMARK]


def custom_kinds(marks: Iterable[Mark], names: dict[str, str] | None = None,
                 recorded: dict[str, str] | None = None) -> dict[str, str]:
    """``meta.json`` ``mark_kinds`` for the user-defined types in use: ``{"risk": "Risk"}``.

    The name is the one the meeting already recorded (``recorded``, its own ``mark_kinds``), else the one in
    Settings now (``names``), else the id made readable. Order: Settings order, then any other type in use
    (a type since removed from Settings), so exports can group custom types in Settings order."""
    used = list(dict.fromkeys(m.kind for m in marks if m.kind not in KIND_NAMES and KIND_PATTERN.match(m.kind)))
    names, recorded = names or {}, recorded or {}
    order = [k for k in names if k in used] + [k for k in used if k not in names]
    return {k: recorded.get(k) or names.get(k) or k.replace("-", " ").capitalize() for k in order}


def summarize(folder: mt.MeetingFolder, names: dict[str, str] | None = None) -> dict:
    """``meta.json`` fields for a meeting's bookmarks: ``marks`` (count) and ``mark_kinds``."""
    state = reduce(read_events(folder.marks_path)) if folder.marks_path.exists() else {}
    try:
        recorded = folder.read_meta().get("mark_kinds")
    except (OSError, ValueError):
        recorded = None
    return {"marks": len(state),
            "mark_kinds": custom_kinds(state.values(), names, recorded if isinstance(recorded, dict) else None)}


# -- the writer --------------------------------------------------------------------
@dataclass(frozen=True)
class MarkResult:
    action: str  # "added" | "ignored" | "retyped"
    id: int = 0
    kind: str = BOOKMARK
    at_ms: int = 0

    def as_dict(self) -> dict:
        return {"action": self.action, "id": self.id, "kind": self.kind, "at_ms": self.at_ms}


class MarkLog:
    """Writes ``marks.jsonl`` of one meeting folder. Safe to call from any thread."""

    def __init__(self, folder: mt.MeetingFolder, clock: Callable[[], float] = time.monotonic,
                 double_press_s: float = DOUBLE_PRESS_S):
        self.folder = folder
        self._clock = clock
        self._double_press_s = double_press_s
        self._lock = threading.Lock()
        self._last: tuple[float, int] | None = None  # (clock time, id) of the last live bookmark
        self._count = len(self._read())

    @property
    def path(self):
        return self.folder.marks_path

    @property
    def count(self) -> int:
        """Bookmarks that exist (deleted ones not counted); cached, so the 200 ms status tick is free."""
        return self._count

    def _events(self) -> list[dict]:
        return read_events(self.path) if self.path.exists() else []

    def _read(self) -> dict[int, Mark]:
        return reduce(self._events())

    def state(self) -> dict[int, Mark]:
        return self._read()

    def _write(self, event: dict, after: str | None) -> str:
        """Append ``event`` stamped with ``changed_at``: when it was written, strictly after ``after`` (the
        file's last ``changed_at``), so the last line says when the bookmarks last changed. Returns the
        stamp. The caller holds the folder lock."""
        stamp = mt.change_time(after)
        mt.append_jsonl_unlocked(self.path, {**event, "changed_at": stamp})
        return stamp

    # -- adding ----------------------------------------------------------------------
    def add(self, kind: str, at_ms: int, via: str = "hotkey", *, seq: int | None = None,
            at: str | None = None) -> MarkResult:
        kind = kind if kind in KIND_NAMES or KIND_PATTERN.match(kind or "") else BOOKMARK
        with self._lock, mt.folder_lock(self.folder.path):
            events = self._events()
            state = reduce(events)
            changed = last_changed(events)
            now = self._clock()
            live = via != "line"
            if live and self._last is not None and now - self._last[0] <= self._double_press_s:
                prev = state.get(self._last[1])
                if prev is not None:
                    if prev.kind == kind:
                        return MarkResult("ignored", prev.id, prev.kind, prev.at_ms)
                    self._write(kind_event(prev.id, kind), changed)
                    self._count = len(state)
                    return MarkResult("retyped", prev.id, kind, prev.at_ms)
            top = max((int(e["id"]) for e in events if isinstance(e.get("id"), int)), default=0)
            mid = top + 1
            self._write(mark_event(mid, kind, at_ms, at or mt.iso(mt.now_local()), via, seq), changed)
            self._count = len(state) + 1
            if live:
                self._last = (now, mid)
            return MarkResult("added", mid, kind, int(at_ms))

    # -- editing ------------------------------------------------------------------------
    def edit(self, mark_id: int, *, label: str | None = None, kind: str | None = None,
             delete: bool = False) -> bool:
        """Write the events that make mark ``mark_id`` match; nothing is written for a part that
        already matches. Returns False when the mark does not exist."""
        with self._lock, mt.folder_lock(self.folder.path):
            events = self._events()
            state = reduce(events)
            changed = last_changed(events)
            cur = state.get(int(mark_id))
            if cur is None:
                return False
            if delete:
                self._write(delete_event(cur.id), changed)
                del state[cur.id]
            else:
                if label is not None and label.strip() != cur.label:
                    changed = self._write(label_event(cur.id, label.strip()), changed)
                if kind is not None and kind != cur.kind and (kind in KIND_NAMES or KIND_PATTERN.match(kind)):
                    self._write(kind_event(cur.id, kind), changed)
            self._count = len(state)
            return True
