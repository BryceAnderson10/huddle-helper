"""What the delete confirmation says (L30), as a pure function of what is selected. No Tk import.

``delete_view(items, audio_only=..., has_bin=..., permanent=...)`` returns the dialog's texts. The rules,
from LIVE_VIEW's "Delete (L30)":

* name what goes: how many meetings (their titles) and how many bytes, or only the kept audio;
* say whether it can come back: "can be restored ... until the Recycle Bin is emptied", and that until
  then the transcript and audio are still on this PC;
* "Permanently" and "cannot be undone" when the user chose permanent (Shift+Delete or the check box),
  and when the drive has no Recycle Bin (then the choice does not exist and there is no check box);
* a stronger warning for a meeting that finished in the last 24 hours (the app that reads the
  transcripts may not have processed it yet): not said about audio, which that app does not read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RECENT_HOURS = 24.0
MAX_NAMES = 3


@dataclass(frozen=True)
class Item:
    """One meeting in the selection."""
    title: str
    bytes: int | None = None  # what would go: the audio folder for audio_only, else the whole folder
    finished_hours: float | None = None  # hours since the meeting finished; None if it has not
    final_failed: bool = False  # the final pass failed (it needs the audio to be retried)
    has_final: bool = True  # a final transcript exists


@dataclass(frozen=True)
class ConfirmView:
    title: str  # window title
    heading: str  # the question
    details: tuple[str, ...]  # what it means
    warnings: tuple[str, ...]  # shown in a warning colour
    confirm: str  # the button that does it
    permanent: bool  # this is the permanent variant (also when forced by a missing Recycle Bin)
    can_choose: bool  # show the "Delete permanently" check box
    cancel_default: bool  # the safe button has focus (permanent, or recent meetings)


def format_size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n} bytes"
    units = ("KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        value /= 1024
        if value < 1024 or unit == units[-1]:
            break
    return f"{value:.0f} {unit}" if value >= 10 or unit == "KB" else f"{value:.1f} {unit}"


def format_age(hours: float) -> str:
    if hours < 1:
        return "less than an hour ago"
    return f"{int(hours)} h ago"


def names(items: list[Item] | tuple[Item, ...], limit: int = MAX_NAMES) -> str:
    titles = [i.title for i in items]
    if len(titles) <= limit:
        return ", ".join(titles)
    return f"{', '.join(titles[:limit])} and {len(titles) - limit} more"


def _total(items) -> int | None:
    sizes = [i.bytes for i in items]
    return None if not sizes or any(s is None for s in sizes) else sum(sizes)


def delete_view(items: list[Item] | tuple[Item, ...], *, audio_only: bool = False, has_bin: bool = True,
                permanent: bool = False) -> ConfirmView:
    """The confirmation for deleting ``items`` (or only their audio). ``permanent`` is the user's choice;
    with no Recycle Bin it is permanent whatever they chose, and the check box is not offered."""
    items = list(items)
    n = len(items)
    perm = permanent or not has_bin
    size = format_size(_total(items))
    sized = f", {size}," if size else ""
    meetings = f"{n} meeting{'' if n == 1 else 's'} ({names(items)})"
    what = f"the kept audio of {meetings}" if audio_only else meetings
    subj = "the audio" if audio_only else ("it" if n == 1 else "they")

    details: list[str] = []
    warnings: list[str] = []
    if perm:
        heading = f"Permanently delete {what}{sized.rstrip(',')}?"
        if has_bin:
            details.append(f"This cannot be undone: {subj} will not go to the Recycle Bin.")
        else:
            details.append(f"This drive has no Recycle Bin, so {subj} cannot be restored.")
    else:
        heading = f"Move {what}{sized} to the Recycle Bin?"
        left = "the audio stays" if audio_only else "the transcript and audio stay"
        details.append(f"{subj.capitalize()} can be restored from there until the Recycle Bin is emptied. "
                       f"Until then {left} on this PC.")
    if audio_only:
        details.append("The transcripts and bookmarks stay; playing lines from the audio will no longer work.")

    if audio_only:
        stuck = [i.title for i in items if i.final_failed]
        if stuck:
            warnings.append(f"The final pass failed for {', '.join(stuck)}. Without the audio it cannot be retried.")
    else:
        recent = [i for i in items if i.finished_hours is not None and i.finished_hours < RECENT_HOURS]
        if recent:
            listed = ", ".join(f"{i.title} ({format_age(i.finished_hours)})" for i in recent[:MAX_NAMES])
            more = f" and {len(recent) - MAX_NAMES} more" if len(recent) > MAX_NAMES else ""
            warnings.append(f"Finished in the last 24 hours: {listed}{more}. The app that reads these meetings "
                            "may not have processed them yet.")
        unfinished = [i.title for i in items if not i.has_final]
        if unfinished:
            warnings.append(f"No final transcript yet: {', '.join(unfinished[:MAX_NAMES])}"
                            f"{' and more' if len(unfinished) > MAX_NAMES else ''}.")

    if audio_only:
        title = "Delete audio"
        confirm = "Delete audio permanently" if perm else "Move audio to Recycle Bin"
    else:
        title = "Delete meeting" if n == 1 else "Delete meetings"
        confirm = "Delete permanently" if perm else "Move to Recycle Bin"
    return ConfirmView(title, heading, tuple(details), tuple(warnings), confirm, perm, has_bin,
                       cancel_default=perm or bool(warnings))
