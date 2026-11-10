"""Speaker names: ``speaker_tags.jsonl`` events, the reducer, and what the final pass does with them
(LIVE_VIEW L11, L12, L14; plan 4.6). No Tk.

Events (append-only, one JSON object per line; see docs/OUTPUT_SPEC.md)

    {"type":"tag","at":"...","scope":"speaker","label":"SPEAKER_04","name":"Eric"}
    {"type":"tag","at":"...","scope":"line","seq":31,"source":"system","start_ms":381200,"end_ms":384900,"name":"Dana"}
    {"type":"untag","at":"...","scope":"speaker","label":"SPEAKER_04"}

``reduce(events)`` applies them in order (later events win) and returns a ``TagState``: names by live
label and names by line, identified by ``(source, start_ms)`` because ``seq`` differs between the live
and the final file. A line name beats a speaker name for that line.

The final pass uses three more functions:

    tagged_ranges(state, live_segments)       every live line the user named, as (source, start, end, name)
    piece_names(pieces, idx, vecs, weights, ranges)
                                              the rules before clustering: a name for each piece
    map_final_speakers(final_segments, ranges)
                                              the mapping after: final label -> name, plus conflicts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from . import meeting as mt

TAG, UNTAG = "tag", "untag"
SPEAKER, LINE = "speaker", "line"

SPEAKER_TAG_GATE = 0.70  # a speaker-level name steers a piece only if it is this much like that name's other pieces
CONFLICT_SHARE = 0.25  # a second name with this share of the winner's overlap is a conflict


# -- events -------------------------------------------------------------------
def tag_speaker_event(label: str, name: str | None, at: str | None = None) -> dict:
    """Name every line with the live label ``label`` (``name`` None or empty: remove the name)."""
    name = (name or "").strip()
    return {"type": TAG if name else UNTAG, "at": at or mt.iso(mt.now_local()), "scope": SPEAKER,
            "label": label, **({"name": name} if name else {})}


def tag_line_event(source: str, start_ms: int, end_ms: int, name: str | None, seq: int | None = None,
                   at: str | None = None) -> dict:
    """Name one live line (``name`` None or empty: remove the line's own name)."""
    name = (name or "").strip()
    event = {"type": TAG if name else UNTAG, "at": at or mt.iso(mt.now_local()), "scope": LINE}
    if seq is not None:
        event["seq"] = int(seq)
    event.update(source=source, start_ms=int(start_ms), end_ms=int(end_ms))
    if name:
        event["name"] = name
    return event


# -- reducer -------------------------------------------------------------------
@dataclass
class TagState:
    speakers: dict[str, str] = field(default_factory=dict)  # live label -> name
    lines: dict[tuple[str, int], tuple[str, int]] = field(default_factory=dict)  # (source, start_ms) -> (name, end_ms)

    def __bool__(self) -> bool:
        return bool(self.speakers or self.lines)

    def name_of(self, label: str, source: str, start_ms: int) -> str | None:
        """What a live line is called: its own name, else its speaker's, else None."""
        line = self.lines.get((source, start_ms))
        return line[0] if line else self.speakers.get(label)

    def names(self) -> list[str]:
        """Every name in use, in order of first use."""
        seen: dict[str, None] = {}
        for n in list(self.speakers.values()) + [v[0] for v in self.lines.values()]:
            seen.setdefault(n)
        return list(seen)


def reduce(events: Iterable[dict], state: TagState | None = None) -> TagState:
    """Apply speaker_tags.jsonl events in order to ``state`` and return it. Malformed events and
    events of other types are ignored. An untag of a name that does not exist is harmless."""
    state = TagState() if state is None else state
    for e in events:
        kind, scope = e.get("type"), e.get("scope")
        if kind not in (TAG, UNTAG):
            continue
        name = str(e.get("name") or "").strip()
        setting = kind == TAG and bool(name)
        if scope == SPEAKER and e.get("label"):
            label = str(e["label"])
            if setting:
                state.speakers[label] = name
            else:
                state.speakers.pop(label, None)
        elif scope == LINE:
            try:
                key = (str(e["source"]), int(e["start_ms"]))
                end = int(e.get("end_ms", key[1]))
            except (KeyError, TypeError, ValueError):
                continue
            if setting:
                state.lines[key] = (name, end)
            else:
                state.lines.pop(key, None)
    return state


# -- ranges ---------------------------------------------------------------------
@dataclass(frozen=True)
class TaggedRange:
    source: str
    start_ms: int
    end_ms: int
    name: str
    scope: str  # "speaker" | "line": which kind of tag named it


def tagged_ranges(state: TagState, live_segments) -> list[TaggedRange]:
    """The live lines the user named, as ranges on their own stream. ``live_segments`` have
    ``source``, ``start_ms``, ``end_ms`` and ``speaker`` (the live label). Line names that match no live
    line (the file was edited by hand) still count, with their own end."""
    out: list[TaggedRange] = []
    seen: set[tuple[str, int]] = set()
    for seg in live_segments:
        key = (seg.source, seg.start_ms)
        seen.add(key)
        line = state.lines.get(key)
        if line:
            out.append(TaggedRange(seg.source, seg.start_ms, seg.end_ms, line[0], LINE))
        elif seg.speaker in state.speakers:
            out.append(TaggedRange(seg.source, seg.start_ms, seg.end_ms, state.speakers[seg.speaker], SPEAKER))
    for key, (name, end) in state.lines.items():
        if key not in seen:
            out.append(TaggedRange(key[0], key[1], max(end, key[1]), name, LINE))
    out.sort(key=lambda r: (r.source, r.start_ms, r.end_ms))
    return out


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


# -- rules before clustering (L14) -----------------------------------------------------
def piece_names(pieces, idx: list[int], vecs: list[np.ndarray], weights: list[float],
                ranges: list[TaggedRange], gate: float = SPEAKER_TAG_GATE) -> list[str | None]:
    """The user's name for each fingerprinted piece, aligned with ``vecs``.

    ``pieces`` are ``(live line index, Segment)``; ``idx[k]`` is the piece behind ``vecs[k]``. A piece
    gets the name of the tagged live range it overlaps most (same stream). A line-level name always
    counts. A speaker-level name counts only if the piece is at least ``gate`` like the weighted centroid
    of all pieces carrying that name (one pass): a live label that mixes two people would otherwise push
    the wrong lines into a cluster. Pieces with no tagged range, or that fail the gate, are ``None``.
    """
    if not ranges or not vecs:
        return [None] * len(vecs)
    by_source: dict[str, list[TaggedRange]] = {}
    for r in ranges:
        by_source.setdefault(r.source, []).append(r)
    cand: list[TaggedRange | None] = []
    for k, i in enumerate(idx):
        seg = pieces[i][1]
        best, best_ov = None, 0
        for r in by_source.get(seg.source, ()):
            if r.start_ms >= seg.end_ms:
                break
            ov = _overlap(seg.start_ms, seg.end_ms, r.start_ms, r.end_ms)
            if ov > best_ov:
                best, best_ov = r, ov
        cand.append(best)
    sums: dict[str, np.ndarray] = {}
    for k, r in enumerate(cand):
        if r is not None:
            sums[r.name] = sums.get(r.name, 0.0) + vecs[k].astype(np.float64) * weights[k]
    cents = {n: v / (np.linalg.norm(v) or 1.0) for n, v in sums.items()}
    out: list[str | None] = []
    for k, r in enumerate(cand):
        if r is None:
            out.append(None)
        elif r.scope == LINE or float(np.dot(vecs[k], cents[r.name])) >= gate:
            out.append(r.name)
        else:
            out.append(None)
    return out


# -- mapping after (L12) -----------------------------------------------------------------
def map_final_speakers(final_segments, ranges: list[TaggedRange], share: float = CONFLICT_SHARE
                       ) -> tuple[dict[str, str], list[dict]]:
    """Name the final speakers from the user's tags.

    For each final speaker (``segment.speaker``) the overlap in ms with each name's tagged ranges on the
    same stream is summed; the largest wins. Any other name with at least ``share`` of the winner's
    overlap makes a conflict: ``{"speaker": label, "names": {name: ms, ...}}`` (all the names in the
    conflict, the winner first). Speakers with no tagged overlap are left out of the mapping.

    One name names one final speaker per stream: the one that overlaps its ranges most. The final pass
    puts every piece it took a name from into one speaker (L39), so a second speaker that overlaps the
    same name only holds pieces a speaker-level name did not fit (``piece_names``' gate), and a few
    seconds of those must not name it.
    """
    by_source: dict[str, list[TaggedRange]] = {}
    for r in ranges:
        by_source.setdefault(r.source, []).append(r)
    totals: dict[str, dict[str, int]] = {}
    source_of: dict[str, str] = {}
    for seg in final_segments:
        for r in by_source.get(seg.source, ()):
            if r.start_ms >= seg.end_ms:
                break
            ov = _overlap(seg.start_ms, seg.end_ms, r.start_ms, r.end_ms)
            if ov:
                per = totals.setdefault(seg.speaker, {})
                per[r.name] = per.get(r.name, 0) + ov
                source_of[seg.speaker] = seg.source
    home: dict[tuple[str, str], tuple[int, str]] = {}  # (stream, name) -> its speaker: most overlap, then label
    for label in sorted(totals):
        for name, ms in totals[label].items():
            key = (source_of[label], name)
            if key not in home or ms > home[key][0]:
                home[key] = (ms, label)
    totals = {label: kept for label, per in totals.items()
              if (kept := {n: ms for n, ms in per.items() if home[(source_of[label], n)][1] == label})}
    mapping: dict[str, str] = {}
    conflicts: list[dict] = []
    for label, per in totals.items():
        ranked = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
        winner, top = ranked[0]
        mapping[label] = winner
        close = {n: ms for n, ms in ranked if ms >= share * top}
        if len(close) > 1:
            conflicts.append({"speaker": label, "names": close})
    conflicts.sort(key=lambda c: c["speaker"])
    return mapping, conflicts
