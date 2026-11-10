"""The attendee list (L13): parse what the user typed at Start, keep it in order, and decide whether the
names also go to speech recognition. No Tk here.

The list is stored in ``meta.json`` as ``attendees`` (additive, only when non-empty). Number keys 1-9 in
the Transcript window mean the first nine names, so the order the user typed is the order that counts.

Names are *not* given to Whisper by default: a wrong name is a wrong fact (TODO.md, WP3 spike: "attendee
names are opt-in only"). The Start dialog has a box for it; ``merge_terms`` is the one place that joins them.
"""

from __future__ import annotations

import re
from typing import Iterable

MAX_ATTENDEES = 30
MAX_NAME_CHARS = 40
KEY_COUNT = 9  # number keys 1-9
RESERVED = {"you", "remote"}  # labels the transcript already uses for the two streams

_SPLIT = re.compile(r"[\r\n,;]+")
_ANGLE = re.compile(r"<[^>]*>|\([^)]*@[^)]*\)")  # "Ana Cole <ana@example.com>" from an invite
_SPACES = re.compile(r"\s+")


def parse_attendees(text: str) -> list[str]:
    """Names from a box: one per line, or separated by commas or semicolons. Email parts in <> are
    dropped, blanks and repeats (ignoring case) are skipped, names longer than ``MAX_NAME_CHARS`` are cut,
    "You" and "Remote" are skipped, and at most ``MAX_ATTENDEES`` are kept."""
    return clean_attendees(_SPLIT.split(text or ""))


def clean_attendees(items: Iterable[object]) -> list[str]:
    """The same rules for a list (what ``meta.json`` holds, or the controller was given)."""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        name = _SPACES.sub(" ", _ANGLE.sub(" ", item)).strip().strip("\"'")
        name = name[:MAX_NAME_CHARS].strip()
        key = name.casefold()
        if not name or key in seen or key in RESERVED or "@" in name:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_ATTENDEES:
            break
    return out


def from_meta(meta: dict | None) -> list[str]:
    """The attendees of a meeting (``[]`` for older meetings and when none were entered)."""
    value = (meta or {}).get("attendees")
    return clean_attendees(value) if isinstance(value, list) else []


def numbered(attendees: list[str]) -> list[tuple[int, str]]:
    """(key, name) for the names the number keys reach: 1..9."""
    return [(i, n) for i, n in enumerate(attendees[:KEY_COUNT], start=1)]


def merge_terms(terms: Iterable[str], attendees: Iterable[str], include_names: bool) -> list[str]:
    """The meeting's vocabulary terms. The typed terms come first; attendee names follow only when the
    user ticked the box. Repeats (ignoring case) are dropped, keeping the first spelling."""
    out: list[str] = []
    seen: set[str] = set()
    for t in [*terms, *(attendees if include_names else ())]:
        t = t.strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out
