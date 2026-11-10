"""Play from here (LIVE_VIEW L19): which clips, in which order, with what silence between them. No Tk,
no device: ``run_sequence`` drives any object with ``rate`` / ``channels`` / ``write`` / ``close`` (the
tests use the fake output of the Player tests).

Rules, in one place:

* **Which lines.** The chosen line and every line after it *at the moment of the click* (a snapshot: a
  run never chases lines that arrive while it plays, so a run cannot feed on its own output), in
  transcript order. Each plays from its own stream, as a single click does. Lines whose audio is not
  available (older than the ring, no file for that stream) are left out; if none is left the run does
  not start. Overlapping lines of the two streams play one after the other, never mixed.
* **Silence.** Each clip carries only ``RUN_PAD_MS`` of padding (120 ms, not the 300 ms of a single click:
  consecutive clips would otherwise play the same sound twice). The pause inserted between two lines is
  the gap between them minus both pads, at most ``MAX_PAUSE_MS`` (400 ms) and never negative: a
  two-minute silence in the meeting plays as a 0.4 s pause.
* **End.** After the last line, on ``cancel`` (Stop, Esc, the play control of the playing line, another
  clip, leaving the meeting, the capture re-opening its devices) or when a device error occurs.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

import numpy as np

from . import playback
from .playback import Clip, PAD_MS

log = logging.getLogger(__name__)

RUN_PAD_MS = 120
MAX_PAUSE_MS = 400
BLOCK_S = 0.1


def pause_ms(prev_end_ms: int, next_start_ms: int, pad_ms: int = RUN_PAD_MS) -> int:
    """The silence to insert between two lines: their gap minus the padding already in the clips, kept
    between 0 and ``MAX_PAUSE_MS``."""
    return int(min(MAX_PAUSE_MS, max(0, next_start_ms - prev_end_ms - 2 * pad_ms)))


def plan_run(rows: Iterable, start_key: str, can_play: Callable[[object], bool] | None = None) -> list[Clip]:
    """The clips of "Play from here" starting at the line whose ``playback.line_key`` is ``start_key``:
    that line and all following lines with text, in row order, minus those ``can_play`` refuses.
    Empty when the start line is not found."""
    rows = list(rows)
    first = next((i for i, r in enumerate(rows) if hasattr(r, "person_key") and hasattr(r, "source")
                  and playback.line_key(r.source, r.start_ms) == start_key), None)
    if first is None:
        return []
    out = []
    for r in rows[first:]:
        if not (hasattr(r, "person_key") and hasattr(r, "source")) or not str(r.text).strip():
            continue
        if can_play is not None and not can_play(r):
            continue
        out.append(replace(playback.line_clip(r), pad_ms=RUN_PAD_MS))
    return out


@dataclass(frozen=True)
class RunResult:
    reason: str  # "finished" | "stopped" | "unavailable" (no clip could be read)
    played: int = 0
    skipped: int = 0  # clips whose audio could not be read when their turn came


def run_sequence(open_out: Callable[[], object], clips: Sequence[Clip], load: Callable[[Clip], np.ndarray | None],
                 cancel: threading.Event, gain: float = 1.0,
                 on_clip: Callable[[Clip, np.ndarray], None] | None = None) -> RunResult:
    """Play ``clips`` on one output stream, opened when the first clip has been read and always closed.

    ``load(clip)`` returns the levelled 16 kHz samples (None: skip it). Each clip is read right after the
    previous one has been handed to the device, so the device still holds its last block while the next
    is read. ``on_clip(clip, samples)`` runs just before a clip's sound starts (after the pause): the app
    moves the highlight there and opens the playback window of the drop rule."""
    out = None
    played = skipped = 0
    prev_end: int | None = None
    try:
        for clip in clips:
            if cancel.is_set():
                return RunResult("stopped", played, skipped)
            samples = load(clip)
            if samples is None or not len(samples):
                skipped += 1
                continue
            if out is None:
                out = open_out()
            if prev_end is not None:
                gap = pause_ms(prev_end, clip.start_ms, clip.pad_ms)
                if gap and not _write(out, np.zeros(int(playback.SAMPLE_RATE * gap / 1000), np.float32), gain, cancel):
                    return RunResult("stopped", played, skipped)
            if cancel.is_set():
                return RunResult("stopped", played, skipped)
            if on_clip is not None:
                on_clip(clip, samples)
            played += 1
            prev_end = max(clip.end_ms, prev_end or 0)
            if not _write(out, samples, gain, cancel):
                return RunResult("stopped", played, skipped)
        return RunResult("finished" if played else "unavailable", played, skipped)
    finally:
        if out is not None:
            try:
                out.close()
            except Exception:  # noqa: BLE001
                log.exception("Could not close the playback device")


def _write(out, samples: np.ndarray, gain: float, cancel: threading.Event) -> bool:
    """Write ``samples`` in blocks; False when cancelled part-way."""
    frames = playback.prepare_pcm(samples, out.rate, out.channels, gain)
    block = max(1, int(out.rate * BLOCK_S))
    for i in range(0, len(frames), block):
        if cancel.is_set():
            return False
        out.write(frames[i:i + block].tobytes())
    return True
