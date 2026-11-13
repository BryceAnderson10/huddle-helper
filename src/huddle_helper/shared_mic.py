"""Shared mic: the stretches of a meeting where several people speak into the user's mic.

``SharedMic`` is a pure, immutable value and the only reader and writer of ``meta.json`` ``shared_mic``
(and of the legacy ``settings.diarize_mic`` that mirrors it). Everything else asks it:

* ``from_meta(meta)`` / ``write(meta)``  read itself (older meetings: ``settings.diarize_mic`` true is one
  stretch over the whole meeting) and write itself back, with ``ended_ms`` once Stop has closed it
* ``at_start(on)`` ``toggled(on, at_ms)`` ``closed_at_stop(end_ms, mic_line_starts)``  the changes
* ``is_open`` ``covers(at_ms)``  "Several people on my mic" is ticked now; a time is inside a stretch
* ``dividers()``  the live view's toggles; the end Stop gave a stretch is not one
* ``mic_seconds(length_s)`` ``spans(n_samples)`` ``separated_streams(diarize_system)``  the final pass

Stretches are ``(start_ms, end_ms | None)`` on the meeting clock, ``None`` while one is open. A mic line
belongs to the stretch it starts in: ``start_ms <= at < end_ms``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .audio import SAMPLE_RATE

Stretch = tuple[int, "int | None"]

# Older meetings (no ``ended_ms``): an end within this of ``duration_s`` is taken for the one Stop gave.
STOP_MARGIN_MS = 1000


def _stretches(raw) -> tuple[Stretch, ...]:
    out = []
    for item in raw:
        if (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], (int, float))
                and (item[1] is None or isinstance(item[1], (int, float)))):
            out.append((int(item[0]), None if item[1] is None else int(item[1])))
    return tuple(out)


def _int_or_none(value, scale: float = 1.0) -> int | None:
    try:
        return int(float(value) * scale)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SharedMic:
    stretches: tuple[Stretch, ...] = ()
    # The meeting's end on the meeting clock, where Stop closed open stretches (meta.json ``ended_ms``).
    ended_ms: int | None = None
    # Older meetings without ``ended_ms``: ``duration_s`` in ms, for the 1 s rule.
    duration_ms: int | None = None

    # -- reading and writing ----------------------------------------------------------------
    @classmethod
    def from_meta(cls, meta: dict) -> "SharedMic":
        raw = meta.get("shared_mic")
        if isinstance(raw, list):
            stretches = _stretches(raw)
        elif (meta.get("settings") or {}).get("diarize_mic"):
            stretches = ((0, None),)  # from before the list existed: the whole mic was shared
        else:
            stretches = ()
        ended = _int_or_none(meta.get("ended_ms"))
        duration = None if ended is not None else _int_or_none(meta.get("duration_s"), 1000)
        return cls(stretches, ended, duration)

    def write(self, meta: dict) -> dict:
        """``meta`` with this value written in: ``shared_mic``, ``settings.diarize_mic`` (true when there is
        at least one stretch; kept for older readers) and, once Stop has closed it, ``ended_ms``."""
        out = {**meta, "shared_mic": [list(st) for st in self.stretches],
               "settings": {**(meta.get("settings") or {}), "diarize_mic": self.any}}
        if self.ended_ms is not None:
            out["ended_ms"] = self.ended_ms
        return out

    # -- changes ---------------------------------------------------------------------------
    @classmethod
    def at_start(cls, on: bool) -> "SharedMic":
        """A new meeting: one stretch from 0 when "Several people on my mic" was ticked at Start."""
        return cls(((0, None),)) if on else cls()

    def toggled(self, on: bool, at_ms: int) -> "SharedMic":
        """Open a stretch at ``at_ms`` (``on``) or close the open one there; unchanged if already so."""
        if bool(on) == self.is_open:
            return self
        at_ms = max(0, int(at_ms))
        if on:
            return replace(self, stretches=self.stretches + ((at_ms, None),))
        start, _ = self.stretches[-1]
        return replace(self, stretches=self.stretches[:-1] + ((start, max(at_ms, start)),))

    def closed_at_stop(self, end_ms: int, mic_line_starts) -> "SharedMic":
        """What Stop leaves: an open stretch ends at ``end_ms`` (the meeting's end on the meeting clock, which
        becomes ``ended_ms``) and a stretch in which no mic line started is dropped."""
        end_ms = int(end_ms)
        starts = list(mic_line_starts)
        closed = [(a, end_ms if b is None else b) for a, b in self.stretches]
        kept = tuple(st for st in closed if any(_inside(st, t) for t in starts))
        return SharedMic(kept, end_ms, None)

    # -- questions -------------------------------------------------------------------------
    @property
    def any(self) -> bool:
        """The meeting has at least one stretch."""
        return bool(self.stretches)

    @property
    def is_open(self) -> bool:
        """A stretch is open now: "Several people on my mic" is ticked."""
        return bool(self.stretches) and self.stretches[-1][1] is None

    def covers(self, at_ms: int) -> bool:
        """``at_ms`` is inside a stretch: a mic line starting there is separated into speakers."""
        return any(_inside(st, at_ms) for st in self.stretches)

    def dividers(self) -> list[tuple[int, bool]]:
        """The live view's dividers ``(at_ms, on)``: one where each stretch starts and one where each ends,
        except the end Stop gave it, which is not a toggle."""
        out = []
        for a, b in self.stretches:
            out.append((a, True))
            if b is not None and not self._ended_by_stop(b):
                out.append((b, False))
        return out

    def _ended_by_stop(self, end_ms: int) -> bool:
        if self.ended_ms is not None:
            return end_ms == self.ended_ms
        return self.duration_ms is not None and end_ms >= self.duration_ms - STOP_MARGIN_MS

    def mic_seconds(self, length_s: float | None) -> float:
        """Seconds of mic audio the final pass separates, in a meeting ``length_s`` long (an open stretch
        runs to its end)."""
        length_ms = int((length_s or 0) * 1000)
        return sum(max(0, min(length_ms if b is None else b, length_ms) - a) for a, b in self.stretches) / 1000

    def spans(self, n_samples: int) -> list[tuple[int, int]]:
        """Sample ranges of the mic audio to separate, one per stretch, in meeting order, clipped to the
        audio. An open stretch runs to the end of the audio; Stop closes one at the meeting's end, which is
        never before the end of the audio, so a stretch over the whole meeting is the whole stream."""
        out = []
        for a, b in sorted(self.stretches, key=lambda st: st[0]):
            start = min(max(a, 0) * SAMPLE_RATE // 1000, n_samples)
            end = n_samples if b is None else min(b * SAMPLE_RATE // 1000, n_samples)
            if end > start:
                out.append((start, end))
        return out

    def separated_streams(self, diarize_system: bool) -> list[str]:
        """The streams the final pass separates into speakers, sorted by name: the mic when the meeting has
        a stretch, the system stream when ``settings.diarize_system``."""
        return [src for src, on in (("mic", self.any), ("system", bool(diarize_system))) if on]


def _inside(stretch: Stretch, at_ms: int) -> bool:
    a, b = stretch
    return a <= at_ms and (b is None or at_ms < b)
