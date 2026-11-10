"""User-defined bookmark types (LIVE_VIEW L28): validation and helpers. No Tk, no I/O.

Settings keep up to ``MAX_TYPES`` entries ``{"id": "risk", "name": "Risk", "hotkey": "Ctrl+Alt+R"}`` (the hotkey
may be ``""``). The **id** is what ``marks.jsonl`` ``kind`` holds (lower-case letters, digits, ``-``); it is made
from the name once, when the type is created, and never changes when the type is renamed. The **name** is
what is shown. Every meeting records the names of the custom types it used in ``meta.json`` ``mark_kinds``
(``{"risk": "Risk"}``), so a type removed from Settings later still shows with its name.

    entries, errors = validate(rows, builtin_hotkeys)    # rows: [(existing id or "", name, hotkey), ...]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from . import hotkeys as hk, marks as marks_mod

MAX_TYPES = 4
NAME_MAX = 24
RESERVED_IDS = frozenset(marks_mod.KIND_NAMES) | frozenset(hk.DEFAULTS)  # a type id is also its hotkey action name


def slugify(name: str) -> str:
    """``"Follow up with legal"`` -> ``"follow-up-with-legal"`` (ASCII letters and digits only; may be empty)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")


def make_id(name: str, taken: Iterable[str]) -> str:
    """A fresh id for ``name`` that is not reserved and not in ``taken``."""
    used = set(taken) | RESERVED_IDS
    base = slugify(name) or "type"
    cand, n = base, 2
    while cand in used:
        cand, n = f"{base}-{n}", n + 1
    return cand


def clean(raw: object) -> list[dict]:
    """Settings.from_dict: the saved list, keeping only well-formed entries (at most ``MAX_TYPES``)."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for e in raw:
        if not isinstance(e, dict):
            continue
        tid, name, key = e.get("id"), e.get("name"), e.get("hotkey", "")
        if (isinstance(tid, str) and marks_mod.KIND_PATTERN.match(tid) and tid not in RESERVED_IDS
                and tid not in seen and isinstance(name, str) and name.strip() and isinstance(key, str)):
            seen.add(tid)
            out.append({"id": tid, "name": name.strip()[:NAME_MAX], "hotkey": key})
        if len(out) >= MAX_TYPES:
            break
    return out


def names(entries: Iterable[dict]) -> dict[str, str]:
    """id -> name, in Settings order."""
    return {e["id"]: e["name"] for e in entries}


def hotkey_bindings(entries: Iterable[dict]) -> dict[str, str]:
    """action name (the type id) -> combo, for the types that have a hotkey."""
    return {e["id"]: e["hotkey"] for e in entries if e.get("hotkey", "").strip()}


@dataclass(frozen=True)
class Problem:
    row: int  # index into the rows given to validate()
    field: str  # "name" | "hotkey"
    text: str


def validate(rows: Iterable[tuple[str, str, str]], other_hotkeys: dict[str, str] | None = None
             ) -> tuple[list[dict] | None, list[Problem]]:
    """Check the rows of the Settings tab. A row with an empty name and an empty hotkey is skipped.

    ``other_hotkeys`` are the combos the other actions use (Settings → Hotkeys); a type's hotkey must differ
    from them and from the other types'. Returns ``(entries, [])`` or ``(None, problems)``.
    """
    rows = list(rows)
    problems: list[Problem] = []
    builtin_names = {n.casefold() for n in marks_mod.KIND_NAMES.values()}
    builtin_names |= {n.casefold() for n in marks_mod.KIND_PLURALS.values()}
    seen_names: dict[str, int] = {}
    combos: dict[str, str] = {}  # canonical combo -> who has it
    for action, text in (other_hotkeys or {}).items():
        try:
            c = hk.normalize(text)
        except ValueError:
            continue
        if c:
            combos.setdefault(c, hk.ACTION_LABELS.get(action, action))
    live = [(i, tid, n.strip(), k.strip()) for i, (tid, n, k) in enumerate(rows) if n.strip() or k.strip()]
    if len(live) > MAX_TYPES:
        problems.append(Problem(live[MAX_TYPES][0], "name", f"At most {MAX_TYPES} types."))
    entries: list[dict] = []
    used_ids = {tid for _, tid, _, _ in live if tid}
    for i, tid, name, key in live:
        bad = False
        if not name:
            problems.append(Problem(i, "name", "Give this type a name, or clear its hotkey."))
            bad = True
        elif len(name) > NAME_MAX:
            problems.append(Problem(i, "name", f"Keep the name to {NAME_MAX} characters or fewer."))
            bad = True
        elif not name.isprintable():
            problems.append(Problem(i, "name", "The name has a character that cannot be shown."))
            bad = True
        elif name.casefold() in builtin_names:
            problems.append(Problem(i, "name", f"“{name}” is already a built-in type."))
            bad = True
        elif name.casefold() in seen_names:
            problems.append(Problem(i, "name", f"“{name}” is used twice."))
            bad = True
        if name:
            seen_names.setdefault(name.casefold(), i)
        canon = ""
        if key:
            try:
                canon = hk.normalize(key)
            except ValueError as exc:
                problems.append(Problem(i, "hotkey", str(exc)))
                bad = True
            else:
                if canon in combos:
                    problems.append(Problem(i, "hotkey", f"{canon} is already used by “{combos[canon]}”."))
                    bad = True
                else:
                    combos[canon] = name or "this type"
        if not bad:
            if not tid:
                tid = make_id(name, used_ids)
                used_ids.add(tid)
            entries.append({"id": tid, "name": name, "hotkey": canon})
    return (None, problems) if problems else (entries, [])
