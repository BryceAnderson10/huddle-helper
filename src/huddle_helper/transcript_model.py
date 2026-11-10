"""UI-free model of one meeting's transcript (plan 4.4).

The Transcript window and the exporter both build on ``TranscriptModel``, so they cannot
disagree about what a meeting looks like. No Tk imports here.

Rows
    ``LineRow``  one transcript segment, with its raw speaker ``label`` and the display ``name``
                 (None when nobody named it); ``person_key`` is ``name or label``.
    ``MarkRow``  a bookmark: ``kind`` is already normalised (an unknown kind reads as ``bookmark``).
    ``GapRow``   a period with no audio from one stream (device change, or ``reason == "muted"``).

Order
    ``model.rows`` is sorted by ``(at_ms, rank, tie)``; rank 0 = line (``start_ms``, tie = seq),
    1 = gap (``start_ms``), 2 = mark (``at_ms``, tie = id). So a line that arrives after a bookmark
    but started before it lands above the bookmark.

Ops
    Every mutator returns a list of ops to replay on a view, in order, each index valid at the
    moment it is applied: ``Insert(index, row)``, ``Update(index, row)``, ``Remove(index)`` and
    ``Reset()`` (throw the view's rows away and redraw from ``model.rows``).

Feeding it (the window tails the files with ``JsonlTail`` and calls these)
    ``apply_transcript(events)``  events of transcript.jsonl or transcript.final.jsonl
    ``apply_tags(events)``        speaker_tags.jsonl events (used for names only while live)
    ``apply_marks(events)``       marks.jsonl events: mark, mark_label, mark_kind, mark_delete
    ``set_meta(meta)``            meta.json dict: title, started_at, status, gaps, mark_kinds
    ``set_speakers(mapping, conflicts=None)``  speakers.json (used for names only once final)
    ``reset_transcript()``        the transcript file was replaced: drop the lines, then re-apply
    ``switch_to_final(events=None, speakers=None, conflicts=None)``
                                  reload from transcript.final.jsonl; returns ``SwapResult(ops,
                                  changed)`` where ``changed`` is the set of ``LineRow.key`` values
                                  whose speaker changed (L3: highlight for 10 s)
    ``TranscriptModel.load(folder)``  build the model for a folder in one go (past meetings, export)

Names (plan 4.4)
    Live file: a line tag beats a speaker tag, which beats the raw label. Line tags are matched on
    ``(source, start_ms)`` (``seq`` is not stable between files). Final file: speakers.json, else the
    raw label. Tags are remembered while final and ignored; speakers.json is remembered while live.

Choices where the plan was silent
    * A segment whose ``seq`` is already in the model is ignored, so replaying events is harmless.
    * Which file: the final file once ``DONE`` exists (and the file does), else the live file and
      ``provisional`` is True (also for a failed final pass).
    * Changed-line detection: a final line with no overlapping live line on its stream is not
      "changed".
    * Mark reducing lives in marks.py (``reduce_marks`` here is the same function).
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Union

from . import marks as marks_mod, tags as tags_mod
from .meeting import MeetingFolder
from .writer import read_events

log = logging.getLogger(__name__)

BOOKMARK, KIND_NAMES, KIND_PLURALS, KIND_ORDER = marks_mod.BOOKMARK, marks_mod.KIND_NAMES,     marks_mod.KIND_PLURALS, marks_mod.KIND_ORDER  # re-exported: export.py and the tests import them here


# -- rows ---------------------------------------------------------------
@dataclass(frozen=True)
class LineRow:
    source: str
    start_ms: int
    end_ms: int
    label: str
    name: str | None
    text: str
    confidence: float
    seq: int

    @property
    def person_key(self) -> str:
        return self.name or self.label

    @property
    def display(self) -> str:
        return self.name or self.label

    @property
    def key(self) -> tuple[str, int]:
        """Identity across the live and final files (seq is not stable)."""
        return (self.source, self.start_ms)

    @property
    def at_ms(self) -> int:
        return self.start_ms


@dataclass(frozen=True)
class MarkRow:
    id: int
    kind: str
    label: str
    at_ms: int

    @property
    def start_ms(self) -> int:
        return self.at_ms


@dataclass(frozen=True)
class GapRow:
    source: str
    start_ms: int
    end_ms: int
    reason: str

    @property
    def at_ms(self) -> int:
        return self.start_ms

    @property
    def muted(self) -> bool:
        return self.reason == "muted"


Row = Union[LineRow, MarkRow, GapRow]


# -- ops ----------------------------------------------------------------
@dataclass(frozen=True)
class Insert:
    index: int
    row: Row


@dataclass(frozen=True)
class Update:
    index: int
    row: Row


@dataclass(frozen=True)
class Remove:
    index: int


@dataclass(frozen=True)
class Reset:
    pass


Op = Union[Insert, Update, Remove, Reset]


@dataclass
class SwapResult:
    ops: list[Op]
    changed: frozenset[tuple[str, int]]  # LineRow.key of final lines whose speaker changed


@dataclass(frozen=True)
class SpeakerInfo:
    person_key: str
    name: str | None
    labels: tuple[str, ...]  # raw labels that map to this person, in order of first appearance
    lines: int
    speech_ms: int


# -- helpers ------------------------------------------------------------
def fmt_elapsed(ms: int) -> str:
    """``00:12:41`` (whole seconds, rounded down)."""
    s = max(0, int(ms)) // 1000
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _sort_key(row: Row) -> tuple:
    if isinstance(row, LineRow):
        return (row.start_ms, 0, row.seq)
    if isinstance(row, GapRow):
        return (row.start_ms, 1, (row.end_ms, row.source))
    return (row.at_ms, 2, row.id)


reduce_marks = marks_mod.reduce  # the reducer lives in marks.py; kept under its old name


def changed_after_swap(live: Iterable[LineRow], final: Iterable[LineRow]) -> set[tuple[str, int]]:
    """Keys of final lines whose speaker differs from the person the live file had there (L3).

    Final labels are renumbered, so labels are not compared directly. Each live label is matched
    to the final label it overlaps most (total ms, same stream); a final line is "changed" when its
    label is not the one its dominant live line (largest overlap) maps to.
    """
    live_by_src: dict[str, list[LineRow]] = {}
    for line in live:
        live_by_src.setdefault(line.source, []).append(line)
    for lines in live_by_src.values():
        lines.sort(key=lambda r: (r.start_ms, r.end_ms))

    totals: Counter = Counter()  # (source, live label, final label) -> overlap ms
    dominant: dict[tuple[str, int], str | None] = {}
    final = list(final)
    lo: dict[str, int] = {}
    for f in sorted(final, key=lambda r: (r.start_ms, r.end_ms)):
        lines = live_by_src.get(f.source, [])
        i = lo.get(f.source, 0)
        while i < len(lines) and lines[i].end_ms <= f.start_ms:
            i += 1
        lo[f.source] = i
        best_ov, best = 0, None
        j = i
        while j < len(lines) and lines[j].start_ms < f.end_ms:
            ov = min(lines[j].end_ms, f.end_ms) - max(lines[j].start_ms, f.start_ms)
            if ov > 0:
                totals[(f.source, lines[j].label, f.label)] += ov
                if ov > best_ov:
                    best_ov, best = ov, lines[j]
            j += 1
        dominant[f.key] = best.label if best is not None else None

    mapping: dict[tuple[str, str], str] = {}
    best_total: dict[tuple[str, str], tuple[int, str]] = {}
    for (src, live_label, final_label), ms in totals.items():
        cand = (ms, final_label)
        if cand > best_total.get((src, live_label), (-1, "")):
            best_total[(src, live_label)] = cand
            mapping[(src, live_label)] = final_label
    changed = set()
    for f in final:
        live_label = dominant.get(f.key)
        if live_label is not None and mapping.get((f.source, live_label)) != f.label:
            changed.add(f.key)
    return changed


# -- the model ----------------------------------------------------------
class TranscriptModel:
    def __init__(self, folder: MeetingFolder | None = None):
        self.folder = folder
        self.final = False  # showing transcript.final.jsonl
        self.rows: list[Row] = []
        self._keys: list[tuple] = []
        self._seqs: set[int] = set()
        self._next_seq = 1
        # names
        self._tags = tags_mod.TagState()
        self._final_names: dict[str, str] = {}
        self.conflicts: list[dict] = []
        # marks and gaps
        self._mark_state: dict[int, marks_mod.Mark] = {}
        self._cur_marks: dict[int, MarkRow] = {}
        self._cur_gaps: dict[tuple, GapRow] = {}
        self.mark_kinds: dict[str, str] = {}  # user-defined kind id -> display name (meta.json)
        # meeting info
        self.meta: dict = {}
        self.meeting_id: str = folder.meeting_id if folder is not None else ""
        self.started_at: str | None = None  # from the transcript's meeting_start, else meta
        self.ended_at: str | None = None
        self.interrupted = False
        self.last_end_ms: int | None = None

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, folder: MeetingFolder) -> "TranscriptModel":
        """Everything for a folder at once: final file if DONE exists, else the live file."""
        m = cls(folder)
        try:
            m.set_meta(folder.read_meta())
        except (OSError, ValueError):
            log.warning("No readable meta.json in %s", folder.path)
        if folder.is_done and folder.final_path.exists():
            m.final = True
            m.set_speakers(folder.read_speakers(), _read_conflicts(folder))
            m.apply_transcript(read_events(folder.final_path))
        elif folder.live_path.exists():
            m.apply_transcript(read_events(folder.live_path))
        if folder.tags_path.exists():
            m.apply_tags(read_events(folder.tags_path))
        if folder.marks_path.exists():
            m.apply_marks(read_events(folder.marks_path))
        return m

    # -- properties -------------------------------------------------------
    @property
    def provisional(self) -> bool:
        """Showing the live file: speaker labels may change after the final pass."""
        return not self.final

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.meeting_id or "Meeting")

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "")

    @property
    def started(self) -> datetime | None:
        return _parse_iso(self.started_at or self.meta.get("started_at"))

    @property
    def ended(self) -> datetime | None:
        return _parse_iso(self.meta.get("ended_at") or self.ended_at)

    @property
    def lines(self) -> list[LineRow]:
        return [r for r in self.rows if isinstance(r, LineRow)]

    @property
    def line_count(self) -> int:
        return sum(1 for r in self.rows if isinstance(r, LineRow))

    @property
    def marks(self) -> list[MarkRow]:
        return [r for r in self.rows if isinstance(r, MarkRow)]

    @property
    def gaps(self) -> list[GapRow]:
        return [r for r in self.rows if isinstance(r, GapRow)]

    @property
    def last_line_end_ms(self) -> int:
        return max((r.end_ms for r in self.rows if isinstance(r, LineRow)), default=0)

    @property
    def duration_ms(self) -> int:
        """Length of the meeting: start to end when both are known and it ended normally, else the
        last audio (interrupted meetings, or still recording)."""
        if self.interrupted:
            return max(self.last_end_ms or 0, self.last_line_end_ms)
        start, end = self.started, self.ended
        if start is not None and end is not None and end >= start:
            return int((end - start).total_seconds() * 1000)
        return self.last_line_end_ms

    def clock(self, ms: int) -> str:
        """Wall-clock time ``HH:MM`` of a meeting time, in the meeting's own UTC offset."""
        start = self.started
        if start is None:
            return fmt_elapsed(ms)[:5]
        return (start + timedelta(milliseconds=int(ms))).strftime("%H:%M")

    def kind_name(self, kind: str) -> str:
        return KIND_NAMES.get(kind) or self.mark_kinds.get(kind) or KIND_NAMES[BOOKMARK]

    def kind_plural(self, kind: str) -> str:
        return KIND_PLURALS.get(kind) or self.mark_kinds.get(kind) or KIND_PLURALS[BOOKMARK]

    def speakers(self) -> list[SpeakerInfo]:
        """One entry per person, in order of first appearance (two labels named alike are one)."""
        order: dict[str, dict] = {}
        for r in self.rows:
            if not isinstance(r, LineRow):
                continue
            e = order.setdefault(r.person_key, {"name": r.name, "labels": [], "lines": 0, "ms": 0})
            if r.label not in e["labels"]:
                e["labels"].append(r.label)
            e["lines"] += 1
            e["ms"] += max(0, r.end_ms - r.start_ms)
        return [SpeakerInfo(k, e["name"], tuple(e["labels"]), e["lines"], e["ms"]) for k, e in order.items()]

    # -- row bookkeeping --------------------------------------------------
    def _insert(self, row: Row) -> Insert:
        key = _sort_key(row)
        i = bisect_left(self._keys, key)
        self._keys.insert(i, key)
        self.rows.insert(i, row)
        return Insert(i, row)

    def _index(self, row: Row) -> int:
        key = _sort_key(row)
        i = bisect_left(self._keys, key)
        if i >= len(self._keys) or self._keys[i] != key:
            raise KeyError(key)
        return i

    def _remove(self, row: Row) -> Remove:
        i = self._index(row)
        del self._keys[i]
        del self.rows[i]
        return Remove(i)

    def _replace(self, old: Row, new: Row) -> list[Op]:
        if _sort_key(old) == _sort_key(new):
            i = self._index(old)
            self.rows[i] = new
            return [Update(i, new)]
        return [self._remove(old), self._insert(new)]

    # -- names ------------------------------------------------------------
    def _name_for(self, label: str, source: str, start_ms: int) -> str | None:
        if self.final:
            return self._final_names.get(label)
        return self._tags.name_of(label, source, start_ms)

    def has_line_tag(self, source: str, start_ms: int) -> bool:
        """The line has a name of its own (a "This line is..." tag), which beats its speaker's name."""
        return (source, start_ms) in self._tags.lines

    def speaker_tag(self, label: str) -> str | None:
        """The name given to the live label as a whole (not the one-off line names), if any."""
        return self._tags.speakers.get(label)

    def line_tag(self, source: str, start_ms: int) -> str | None:
        """The line's own name, if it has one."""
        hit = self._tags.lines.get((source, start_ms))
        return hit[0] if hit else None

    def _refresh_names(self) -> list[Op]:
        ops: list[Op] = []
        for i, r in enumerate(self.rows):
            if isinstance(r, LineRow):
                name = self._name_for(r.label, r.source, r.start_ms)
                if name != r.name:
                    new = LineRow(r.source, r.start_ms, r.end_ms, r.label, name, r.text, r.confidence, r.seq)
                    self.rows[i] = new
                    ops.append(Update(i, new))
        return ops

    def apply_tags(self, events: Iterable[dict]) -> list[Op]:
        tags_mod.reduce(events, self._tags)
        return [] if self.final else self._refresh_names()

    def set_speakers(self, mapping: dict[str, str], conflicts: list[dict] | None = None) -> list[Op]:
        self._final_names = {str(k): str(v).strip() for k, v in (mapping or {}).items()
                             if v and str(v).strip() and str(v).strip() != str(k)}
        self.conflicts = list(conflicts or [])
        return self._refresh_names() if self.final else []

    # -- transcript -------------------------------------------------------
    def _line_from_event(self, e: dict) -> LineRow | None:
        try:
            source, start, end = str(e["source"]), int(e["start_ms"]), int(e["end_ms"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            seq = int(e["seq"])
        except (KeyError, TypeError, ValueError):
            seq = self._next_seq
        label = str(e.get("speaker") or "")
        return LineRow(source, start, end, label, self._name_for(label, source, start),
                       str(e.get("text") or ""), float(e.get("confidence") or 0.0), seq)

    def apply_transcript(self, events: Iterable[dict]) -> list[Op]:
        ops: list[Op] = []
        for e in events:
            kind = e.get("type")
            if kind == "segment":
                row = self._line_from_event(e)
                if row is None or row.seq in self._seqs:
                    continue
                self._seqs.add(row.seq)
                self._next_seq = max(self._next_seq, row.seq + 1)
                ops.append(self._insert(row))
            elif kind == "meeting_start":
                self.meeting_id = str(e.get("meeting_id") or self.meeting_id)
                self.started_at = e.get("started_at") or self.started_at
            elif kind == "meeting_end":
                self.ended_at = e.get("ended_at") or self.ended_at
                self.interrupted = bool(e.get("interrupted"))
                self.last_end_ms = e.get("last_end_ms")
        return ops

    def reset_transcript(self) -> list[Op]:
        """The transcript file was replaced or truncated: drop its lines (marks and gaps stay)."""
        keep = [r for r in self.rows if not isinstance(r, LineRow)]
        self.rows = keep
        self._keys = [_sort_key(r) for r in keep]
        self._seqs.clear()
        self._next_seq = 1
        self.ended_at, self.interrupted, self.last_end_ms = None, False, None
        return [Reset()]

    def switch_to_final(self, events: list[dict] | None = None, speakers: dict[str, str] | None = None,
                        conflicts: list[dict] | None = None) -> SwapResult:
        """Replace the lines with the final file's. With no arguments it reads the folder's
        transcript.final.jsonl and speakers.json."""
        if events is None:
            if self.folder is None:
                raise ValueError("switch_to_final needs events or a folder")
            events = read_events(self.folder.final_path)
            if speakers is None:
                speakers = self.folder.read_speakers()
                conflicts = _read_conflicts(self.folder)
        old_lines = self.lines
        self.reset_transcript()
        self.final = True
        self.set_speakers(speakers or {}, conflicts)
        self.apply_transcript(events)
        changed = changed_after_swap(old_lines, self.lines)
        return SwapResult([Reset()], frozenset(changed))

    # -- meta, gaps, marks -------------------------------------------------
    def set_meta(self, meta: dict) -> list[Op]:
        self.meta = dict(meta or {})
        kinds = self.meta.get("mark_kinds")
        self.mark_kinds = {str(k): str(v) for k, v in kinds.items()} if isinstance(kinds, dict) else {}
        desired: dict[tuple, GapRow] = {}
        for g in self.meta.get("gaps") or []:
            try:
                row = GapRow(str(g["source"]), int(g["start_ms"]), int(g["end_ms"]), str(g.get("reason") or ""))
            except (KeyError, TypeError, ValueError):
                continue
            desired[(row.source, row.start_ms, row.end_ms)] = row
        ops = self._sync(self._cur_gaps, desired)
        return ops + self._sync_marks()

    def apply_marks(self, events: Iterable[dict]) -> list[Op]:
        reduce_marks(events, self._mark_state)
        return self._sync_marks()

    def _sync_marks(self) -> list[Op]:
        desired = {mid: MarkRow(mid, m.read_kind(self.mark_kinds), m.label, m.at_ms)
                   for mid, m in self._mark_state.items()}
        return self._sync(self._cur_marks, desired)

    def _sync(self, current: dict, desired: dict) -> list[Op]:
        ops: list[Op] = []
        for k in [k for k in current if k not in desired]:
            ops.append(self._remove(current.pop(k)))
        for k, row in desired.items():
            old = current.get(k)
            if old is None:
                ops.append(self._insert(row))
            elif old != row:
                ops.extend(self._replace(old, row))
            current[k] = row
        return ops


def _read_conflicts(folder: MeetingFolder) -> list[dict]:
    from .meeting import read_json

    try:
        data = read_json(folder.speakers_path)
    except (OSError, ValueError):
        return []
    conflicts = data.get("conflicts") if isinstance(data, dict) else None
    return [c for c in conflicts if isinstance(c, dict)] if isinstance(conflicts, list) else []
