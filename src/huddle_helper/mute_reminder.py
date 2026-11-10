"""The "still muted" reminder (LIVE_VIEW L24). No Tk, no I/O; the clock is injected.

    r = MuteReminder(interval_s=600, clock=time.monotonic)
    due = r.poll(muted)      # call often; returns the seconds muted (a multiple of the interval) when a
                             # reminder is due now, else None

The count starts when ``poll`` first sees the mic muted and starts over whenever it sees it unmuted, so only
*continuous* mute is counted. A reminder comes every ``interval_s`` after that; if polling stalled across
several intervals one reminder is returned (for the latest multiple), never a burst. ``reset()`` forgets
everything (Stop).
"""

from __future__ import annotations

import time
from typing import Callable

INTERVAL_S = 600.0


class MuteReminder:
    def __init__(self, interval_s: float = INTERVAL_S, clock: Callable[[], float] = time.monotonic):
        self.interval_s = max(0.001, float(interval_s))
        self._clock = clock
        self._since: float | None = None
        self._next = 0.0

    def poll(self, muted: bool) -> float | None:
        now = self._clock()
        if not muted:
            self._since = None
            return None
        if self._since is None:
            self._since, self._next = now, now + self.interval_s
            return None
        if now < self._next:
            return None
        n = int((now - self._since) // self.interval_s)
        self._next = self._since + (n + 1) * self.interval_s
        return n * self.interval_s

    def muted_for_s(self) -> float:
        """Seconds of the current continuous mute (0 when not muted)."""
        return 0.0 if self._since is None else self._clock() - self._since

    def reset(self) -> None:
        self._since = None


def duration_text(seconds: float) -> str:
    """``"20 min"``; under a minute (only the test interval gets there) ``"45 s"``."""
    s = int(round(seconds))
    return f"{s} s" if s < 60 else f"{max(1, round(s / 60))} min"


def reminder_message(seconds: float) -> str:
    return f"Your mic is still muted in the recording ({duration_text(seconds)})."
