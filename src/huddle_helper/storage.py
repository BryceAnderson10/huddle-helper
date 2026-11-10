"""The Storage panel (L32): disk use of the meetings folder and "Delete kept audio older than N days".

No Tk, no disk access: everything works on the ``library.MeetingInfo`` rows the Meetings view already
has (sizes are measured by the library's worker thread, so a large folder never blocks Tk).

* ``usage``        total size split into audio and everything else, and the meetings with the most audio
* ``plan_purge``   which meetings' kept audio is older than N days: the *previewed set*. The panel deletes
                   exactly ``plan.ids`` and nothing it did not show.
* ``usage_lines`` ``purge_summary`` ``setting_text``   the panel's wording

The age rule is the one auto-purge uses (``housekeeping.purge_old_audio``): a finished meeting (DONE exists)
that still has an ``audio\\`` folder, aged by the modification time of its DONE file. A meeting that is
recording, queued or being processed is never offered. Moving to the Recycle Bin versus deleting for good is
decided by the caller, as for the Meetings list (``library.delete_audio``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import library
from .gui import confirm
from .library import LiveState, MeetingInfo

LARGEST = 5  # meetings listed under "Largest by kept audio"
DAY_S = 86400.0
DEFAULT_DAYS = 30  # prefilled in "older than N days" when auto-purge is off


@dataclass(frozen=True)
class Usage:
    meetings: int
    total_bytes: int
    audio_bytes: int
    with_audio: int  # meetings that still have an audio folder
    measured: bool  # every meeting has been measured (else the numbers are lower bounds)
    largest: tuple[MeetingInfo, ...]  # by kept audio, biggest first

    @property
    def other_bytes(self) -> int:
        """Transcripts, bookmarks and everything else that is not the audio folder."""
        return max(0, self.total_bytes - self.audio_bytes)


def _has_audio(info: MeetingInfo) -> bool:
    """An audio folder with something in it (an empty one frees nothing, so it is neither counted nor offered)."""
    return info.has_audio and info.audio_bytes != 0


def usage(infos: list[MeetingInfo], top: int = LARGEST) -> Usage:
    t = library.totals(infos)
    audio = [i for i in infos if _has_audio(i)]
    audio.sort(key=lambda i: (-(i.audio_bytes or 0), i.id))
    return Usage(t.meetings, t.total_bytes, t.audio_bytes, len(audio), t.measured, tuple(audio[:top]))


@dataclass(frozen=True)
class PurgeItem:
    id: str
    title: str
    age_days: float
    audio_bytes: int | None
    done_at: float  # when the final transcript was finished (DONE's modification time)


@dataclass(frozen=True)
class PurgePlan:
    """What "Delete kept audio older than ``days`` days" would delete right now."""
    days: int
    items: tuple[PurgeItem, ...]
    complete: bool  # every candidate's audio size is known (and so is the total)

    @property
    def ids(self) -> list[str]:
        return [i.id for i in self.items]

    @property
    def total_bytes(self) -> int:
        return sum(i.audio_bytes or 0 for i in self.items)

    @property
    def empty(self) -> bool:
        return not self.items


def plan_purge(infos: list[MeetingInfo], days: int, live: LiveState | None = None,
               now: float | None = None) -> PurgePlan:
    """The meetings whose kept audio is older than ``days`` days (``days`` of 0 or less selects nothing, as the
    auto-purge setting does). Oldest first."""
    if days <= 0:
        return PurgePlan(max(0, days), (), True)
    now = time.time() if now is None else now
    cutoff = now - days * DAY_S
    items = []
    for info in infos:
        if not (info.done and _has_audio(info)) or info.done_at is None or info.done_at > cutoff:
            continue
        if library.status_of(info, live).busy:
            continue
        items.append((info.done_at, PurgeItem(info.id, info.title, (now - info.done_at) / DAY_S, info.audio_bytes,
                                              info.done_at)))
    items.sort(key=lambda p: (p[0], p[1].id))
    chosen = tuple(p[1] for p in items)
    return PurgePlan(days, chosen, all(i.audio_bytes is not None for i in chosen))


# -- wording ------------------------------------------------------------------------------------------------
def _meetings(n: int) -> str:
    return f"{n} meeting{'' if n == 1 else 's'}"


def usage_lines(u: Usage) -> tuple[str, str]:
    """(headline, split): "12 meetings, 2.1 GB" and "Kept audio 1.9 GB (9 meetings) · transcripts and the rest 200 MB"."""
    if not u.meetings:
        return "No meetings yet.", ""
    if not u.measured:
        return f"{_meetings(u.meetings)} · measuring sizes…", ""
    head = f"{_meetings(u.meetings)}, {confirm.format_size(u.total_bytes)}"
    split = (f"Kept audio {confirm.format_size(u.audio_bytes)} ({_meetings(u.with_audio)}) · "
             f"transcripts and the rest {confirm.format_size(u.other_bytes)}")
    return head, split


def setting_text(days: int) -> str:
    return (f"Kept audio is deleted automatically {days} day{'' if days == 1 else 's'} after a meeting finished."
            if days > 0 else "Automatic deletion of kept audio is off.")


def purge_summary(plan: PurgePlan) -> str:
    """"6 meetings, 1.2 GB of kept audio is older than 30 days." (the preview's headline)"""
    if plan.empty:
        return f"No kept audio is older than {plan.days} day{'' if plan.days == 1 else 's'}."
    size = confirm.format_size(plan.total_bytes) if plan.complete else f"at least {confirm.format_size(plan.total_bytes)}"
    return (f"{_meetings(len(plan.items))}, {size}: kept audio older than "
            f"{plan.days} day{'' if plan.days == 1 else 's'}.")


def confirm_items(plan: PurgePlan) -> list[confirm.Item]:
    """The plan for ``confirm.delete_view(..., audio_only=True)`` (the same wording as Delete audio)."""
    return [confirm.Item(i.title, i.audio_bytes, None) for i in plan.items]
