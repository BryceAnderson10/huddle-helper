"""Meeting export to Markdown and plain text (LIVE_VIEW "Export"; pure functions, no Tk).

    text = render(model, fmt="md" | "txt", times="elapsed" | "clock", join=True, crlf=None)
    text = export_folder(folder, fmt, times, join)       # loads the model first
    name = export_filename(model, fmt)                   # "<meeting_id>.md"
    write_export(path, text)                             # UTF-8, text written exactly as given

``model`` is a ``TranscriptModel``; the window passes the one it is already showing, the CLI
loads one with ``TranscriptModel.load``, so names (speakers.json for the final file, tags for the
live file), marks and gaps are the same everywhere.

Choices where LIVE_VIEW is silent
    * ``crlf=None`` means "Windows line endings for ``.txt``, ``\\n`` for ``.md``". Pass
      ``crlf=False`` for text that goes to the Tk clipboard (Tk converts newlines itself).
    * The provisional banner ("Provisional: speaker labels may change") is the first line of the
      file, for a live-file export. An interrupted meeting gets a one-line note under the header.
    * Join: consecutive lines of one person (``person_key``) form a paragraph with the first line's
      time. A device or mute gap between two lines ends the paragraph, so the gap line stays where it
      happened.
    * A bookmark goes before the first paragraph or gap that starts after its ``at_ms``: after the
      paragraph that contains it, or, in a pause, before the next one. Several bookmarks keep their
      ``(at_ms, id)`` order.
    * Clock times are ``HH:MM`` in the meeting's own UTC offset, as LIVE_VIEW shows (``11:12``).
    * Markdown special characters in speaker names, labels and text are backslash-escaped.
    * Gap wording: ``muted`` reads "you muted your mic in the recording"; any other reason reads
      "no audio from your mic / the call (reason)".
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import status as status_mod
from .meeting import MeetingFolder
from .transcript_model import KIND_ORDER, DividerRow, GapRow, MarkRow, TranscriptModel, fmt_elapsed

PROVISIONAL = "Provisional: speaker labels may change"
INTERRUPTED = ("Recovered after an interruption: the recording stopped unexpectedly, "
               "so the end of the meeting may be missing.")
EXTENSIONS = {"md": "md", "txt": "txt"}

# "_" inside a word (SPEAKER_01, snake_case) does not start emphasis in Markdown, so it stays readable.
_MD_SPECIAL = re.compile(r"([\\*`\[\]<>]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9]))")


def _esc(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _duration(ms: int) -> str:
    s = max(0, ms) / 1000
    return f"{max(1, round(s))} s" if s < 60 else f"{round(s / 60)} min"


@dataclass
class _Item:
    start_ms: int
    kind: str  # "para" | "gap"
    who: str = ""
    text: str = ""
    gap: GapRow | None = None


def _items(model: TranscriptModel, join: bool) -> list[_Item]:
    items: list[_Item] = []
    last_key: str | None = None  # person_key of the paragraph being extended
    for row in model.rows:
        if isinstance(row, (MarkRow, DividerRow)):  # dividers are the live view's, not the transcript's
            continue
        if isinstance(row, GapRow):
            items.append(_Item(row.start_ms, "gap", gap=row))
            last_key = None
            continue
        text = row.text.strip()
        if join and last_key == row.person_key and items and items[-1].kind == "para":
            items[-1].text = f"{items[-1].text} {text}".strip()
            continue
        items.append(_Item(row.start_ms, "para", who=row.display, text=text))
        last_key = row.person_key
    return items


def _speaker_names(model: TranscriptModel) -> list[str]:
    return [s.name or s.labels[0] for s in model.speakers()]


def _header(model: TranscriptModel) -> tuple[str, str]:
    """(``2026-11-09, 11:00–11:42 (42 min)``, ``You, Dana, Eric``) either part may be empty."""
    start = model.started
    dur = model.duration_ms
    parts = []
    if start is not None:
        end = start + timedelta(milliseconds=dur)
        parts.append(f"{start:%Y-%m-%d}, {start:%H:%M}–{end:%H:%M}")
    if dur > 0:
        parts.append(f"({max(1, round(dur / 60000))} min)")
    when = " ".join(parts)
    return when, ", ".join(_speaker_names(model))


def _mark_groups(marks: list[MarkRow], model: TranscriptModel) -> list[tuple[str, list[MarkRow]]]:
    by_kind: dict[str, list[MarkRow]] = {}
    for m in marks:
        by_kind.setdefault(m.kind, []).append(m)
    # the built-in four, then user-defined types in the order meta.json lists them (Settings order)
    order = [k for k in KIND_ORDER if k in by_kind]
    order += [k for k in model.mark_kinds if k in by_kind and k not in order]
    order += [k for k in by_kind if k not in order]
    return [(model.kind_plural(k), by_kind[k]) for k in order]


def render(model: TranscriptModel, fmt: str = "md", times: str = "elapsed", join: bool = True,
           crlf: bool | None = None) -> str:
    if fmt not in EXTENSIONS:
        raise ValueError(f"unknown export format {fmt!r} (use md or txt)")
    if times not in ("elapsed", "clock"):
        raise ValueError(f"unknown times {times!r} (use elapsed or clock)")
    md = fmt == "md"
    stamp = fmt_elapsed if times == "elapsed" else model.clock
    items = _items(model, join)
    marks = sorted((r for r in model.rows if isinstance(r, MarkRow)), key=lambda m: (m.at_ms, m.id))
    starts = [it.start_ms for it in items]
    before: dict[int, list[MarkRow]] = {}  # item index -> marks to print before it
    for m in marks:
        before.setdefault(bisect_right(starts, m.at_ms), []).append(m)

    def mark_block(m: MarkRow) -> str:
        kind, when = model.kind_name(m.kind), stamp(m.at_ms)
        if md:
            body = f"{_esc(kind)} {when}" + (f": {_esc(m.label)}" if m.label else "")
            return f'<a id="bookmark-{m.id}"></a>\n> **{body}**'
        return f"---- {kind} {when}" + (f": {m.label}" if m.label else "") + " ----"

    def gap_block(g: GapRow) -> str:
        a, b = stamp(g.start_ms), stamp(g.end_ms)
        dur = _duration(g.end_ms - g.start_ms)
        what = "your mic" if g.source == "mic" else "the call"
        if g.muted:
            what_happened = "you muted your mic in the recording"
        else:
            what_happened = f"no audio from {what}" + (f" ({g.reason})" if g.reason else "")
        return f"*{a}–{b}: {what_happened} ({dur}).*" if md else f"({a}-{b} {what_happened}, {dur})"

    def para_block(it: _Item) -> str:
        if md:
            return f"**{stamp(it.start_ms)} {_esc(it.who)}:** {_esc(it.text)}"
        return f"[{stamp(it.start_ms)}] {it.who}: {it.text}"

    body: list[str] = []
    for i in range(len(items) + 1):
        for m in before.get(i, []):
            body.append(mark_block(m))
        if i < len(items):
            it = items[i]
            body.append(para_block(it) if it.kind == "para" else gap_block(it.gap))
    if not body:
        body.append("*No transcript lines.*" if md else "(no transcript lines)")

    when, who = _header(model)
    recovered = status_mod.recovered(model.meta)
    out: list[str] = []
    if model.provisional:
        out += [f"> {PROVISIONAL}" if md else PROVISIONAL, ""]
    if md:
        out += [f"# {_esc(model.title)}", ""]
        line = " · ".join(p for p in (when, f"Speakers: {_esc(who)}" if who else "") if p)
        if line:
            out += [line, ""]
        if recovered:
            out += [f"> {INTERRUPTED}", ""]
        if marks:
            out += ["## Bookmarks", ""]
            for heading, group in _mark_groups(marks, model):
                out += [f"### {_esc(heading)}"]
                for m in group:
                    text = stamp(m.at_ms) + (f" {_esc(m.label)}" if m.label else "")
                    out.append(f"- [{text}](#bookmark-{m.id})")
                out.append("")
        out += ["## Transcript", ""]
        out += ["\n\n".join(body)]
    else:
        out += [model.title]
        if when:
            out.append(when)
        if who:
            out.append(f"Speakers: {who}")
        if recovered:
            out.append(INTERRUPTED)
        out.append("")
        out += body
    text = "\n".join(out) + "\n"
    if crlf is None:
        crlf = not md
    return text.replace("\n", "\r\n") if crlf else text


def export_folder(folder: MeetingFolder, fmt: str = "md", times: str = "elapsed", join: bool = True,
                  crlf: bool | None = None) -> str:
    return render(TranscriptModel.load(folder), fmt, times, join, crlf)


def export_filename(model: TranscriptModel, fmt: str) -> str:
    return f"{model.meeting_id}.{EXTENSIONS[fmt]}"


def write_export(path: Path | str, text: str) -> None:
    """Write UTF-8 exactly as given (no newline translation, so a .txt keeps its CRLF)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
