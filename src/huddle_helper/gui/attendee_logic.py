"""Attendees in the Transcript window (L13): name choices, the number keys, the undo, the "Who is this?"
prompt for new voices. No Tk here; ``window.py`` draws it.

Behaviour fixed here, and in docs/LIVE_VIEW.md:

* **Choices.** Attendees not yet used in the meeting first (in the order typed), then the names already in
  use. The same list feeds the name picker and the "This line is..." submenu.
* **Number keys 1-9** name the *latest call line's speaker* as attendee N. "Latest" is the most recent
  ``system`` line whose speaker is still unnamed; when every call speaker is named, the most recent
  ``system`` line. While a "Who is this?" bar is showing, the keys answer *it* (the voice it asks about),
  so the number always goes to the voice the user is looking at. They are live only in the live view of a meeting that is recording or being processed,
  only with attendees, and never while a text box has the focus.
* **Undo.** Each naming made from the keys or the prompt can be undone from the confirmation: the labels
  and lines it touched go back to the tag they had.
* **Who is this?** A call label seen for the first time (and not named) is queued; one prompt shows at a
  time; naming the voice any other way, or dismissing the prompt, moves to the next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import attendees as att
from ..transcript_model import LineRow, TranscriptModel
from . import logic

CALL = "system"
PROMPT_BUTTONS = 5  # attendee buttons in the prompt; the picker ("Other…") has the whole list
SNIPPET_CHARS = 48
# widget classes where typing is text entry; a Text counts only when it is not read-only
ENTRY_CLASSES = frozenset({"Entry", "TEntry", "TCombobox", "Combobox", "Spinbox", "TSpinbox"})
ALT_MASK, CONTROL_MASK = 0x20000, 0x4  # Tk key-event state bits on Windows


# -- choices ------------------------------------------------------------------------
def used_names(model: TranscriptModel) -> set[str]:
    """Names (casefolded) already given to a person in this meeting."""
    return {s.name.casefold() for s in model.speakers() if s.name}


def pick_names(model: TranscriptModel, attendees: list[str], exclude: str | None = None) -> list[str]:
    """For the name picker and "This line is…": attendees not used yet, then names in use (dropping
    ``exclude``, the current name), each once, ignoring case."""
    used = used_names(model)
    out: list[str] = []
    seen: set[str] = set()
    if exclude:
        seen.add(exclude.casefold())
    for name in [*(a for a in attendees if a.casefold() not in used), *logic.name_choices(model, exclude)]:
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


# -- number keys --------------------------------------------------------------------
def digit_of(char: str, state: int = 0) -> int | None:
    """1-9 for a plain digit key press; None for anything else (Ctrl or Alt held, 0, letters)."""
    if state & (CONTROL_MASK | ALT_MASK) or len(char or "") != 1 or char not in "123456789":
        return None
    return int(char)


def focus_blocks_keys(widget_class: str, text_state: str = "normal") -> bool:
    """True when the focused widget takes typed text, so the number keys must leave it alone. The
    transcript itself is a read-only Text (``text_state`` "disabled") and does not block."""
    if widget_class in ENTRY_CLASSES:
        return True
    return widget_class == "Text" and text_state != "disabled"


def keys_enabled(phase: str, model: TranscriptModel, attendees: list[str]) -> bool:
    """Number keys work in the live view of a meeting being recorded or processed, with attendees."""
    return bool(attendees) and not model.final and phase in (logic.RECORDING, logic.STOPPING, logic.PROCESSING)


def key_target(model: TranscriptModel) -> LineRow | None:
    """The latest call line whose speaker is unnamed; else the latest call line; else None."""
    lines = [r for r in model.lines if r.source == CALL]
    for r in reversed(lines):
        if r.name is None and r.label not in logic.KNOWN_LABELS:
            return r
    return lines[-1] if lines else None


def key_voice(model: TranscriptModel, prompted: "NewVoice | None") -> tuple[str, str] | None:
    """(person_key, display) the number keys name: the voice of the "Who is this?" bar when one is showing,
    else the speaker of ``key_target``."""
    if prompted is not None:
        return prompted.person_key, prompted.display
    row = key_target(model)
    return (row.person_key, row.display) if row is not None else None


# -- undo ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NamingUndo:
    """What a naming touched and the tags to put back (``None``: no tag)."""
    labels: tuple[tuple[str, str | None], ...] = ()
    lines: tuple[tuple[LineRow, str | None], ...] = ()


def capture_undo(model: TranscriptModel, person_key: str) -> NamingUndo:
    plan = logic.name_plan(model, person_key)
    return NamingUndo(tuple((label, model.speaker_tag(label)) for label in plan.labels),
                      tuple((row, model.line_tag(row.source, row.start_ms)) for row in plan.lines))


def confirmation_text(before: str, name: str) -> str:
    return f"{before} is now {name}"


# -- the strip ----------------------------------------------------------------------
def strip_text(model: TranscriptModel, attendees: list[str], prompted: "NewVoice | None" = None) -> str:
    """The numbered attendee line: "Keys 1–9 name SPEAKER_03:  1 Ana ✓ · 2 Ben"; "" with no attendees."""
    if not attendees:
        return ""
    used = used_names(model)
    target = key_voice(model, prompted)
    head = f"Keys 1–{min(len(attendees), att.KEY_COUNT)} name " + (target[1] if target else "the next new voice")
    parts = [f"{n} {name}" + (" ✓" if name.casefold() in used else "") for n, name in att.numbered(attendees)]
    text = f"{head}:  " + "   ".join(parts)
    extra = len(attendees) - att.KEY_COUNT
    return text + (f"   +{extra} more in the name list" if extra > 0 else "")


# -- "Who is this?" for new voices ---------------------------------------------------
@dataclass(frozen=True)
class NewVoice:
    label: str
    person_key: str
    display: str
    snippet: str


@dataclass(frozen=True)
class PromptButton:
    label: str
    action: str  # "pick:<n>" | "other" | "dismiss" | "undo"


@dataclass(frozen=True)
class PromptView:
    text: str
    buttons: tuple[PromptButton, ...]
    inline: bool = False  # buttons on the text's row (short confirmations) instead of under it


def prompt_view(voice: NewVoice, attendees: list[str], used: set[str]) -> PromptView:
    """"New voice SPEAKER_03 (“Can everyone hear me?”). Who is this?" with the unused attendees (their
    own numbers, as in the strip), "Other…" and "Not now"."""
    quote = f" (“{voice.snippet}”)" if voice.snippet else ""
    buttons = [PromptButton(f"{n} {name}", f"pick:{n}") for n, name in att.numbered(attendees)
               if name.casefold() not in used][:PROMPT_BUTTONS]
    buttons += [PromptButton("Other…", "other"), PromptButton("Not now", "dismiss")]
    return PromptView(f"New voice {voice.display}{quote}. Who is this?", tuple(buttons))


def undo_view(text: str) -> PromptView:
    return PromptView(text, (PromptButton("Undo", "undo"), PromptButton("OK", "dismiss")), inline=True)


@dataclass
class WhoPrompter:
    """Finds new unnamed call voices and hands them out one at a time. Call ``update`` on every refresh."""
    seen: set[str] = field(default_factory=set)
    queue: list[NewVoice] = field(default_factory=list)
    current: NewVoice | None = None

    def reset(self) -> None:
        self.seen, self.queue, self.current = set(), [], None

    def update(self, model: TranscriptModel, active: bool) -> NewVoice | None:
        """``active``: the live view of a meeting being recorded or processed. Returns the voice to ask
        about (None: no prompt)."""
        if not active:
            self.queue, self.current = [], None
            return None
        for r in model.lines:
            if r.source != CALL or r.label in self.seen:
                continue
            self.seen.add(r.label)
            if r.label not in logic.KNOWN_LABELS and model.speaker_tag(r.label) is None:
                self.queue.append(NewVoice(r.label, r.label, r.label,
                                           logic.short_name(r.text.strip(), SNIPPET_CHARS)))
        self.queue = [v for v in self.queue if model.speaker_tag(v.label) is None]
        if self.current is not None and model.speaker_tag(self.current.label) is not None:
            self.current = None  # named some other way
        if self.current is None and self.queue:
            self.current = self.queue.pop(0)
        return self.current

    def dismiss(self) -> None:
        self.current = None
