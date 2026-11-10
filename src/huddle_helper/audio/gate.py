"""The mic gate (plan 4.7, LIVE_VIEW "Mute me in the recording"). No Tk, no I/O.

``MicGate.process(samples)`` sits on the capture path, right where the mic's 16 kHz audio enters the
recorder, and returns the chunk unchanged (the same array, nothing copied) while the mic is open or a
chunk of zeros while it is muted. Everything downstream (FLAC buffer, level meter, VAD, transcription,
fingerprints) only ever sees the gated samples, so muted audio is never written or analysed.

The gate counts the mic samples that pass, and all times are sample counts (16 samples = 1 ms), which
is the meeting clock because the aligner pads the mic stream to it. So the muted period it reports
matches the silence in ``mic.flac`` exactly:

    mute()    zeroes from the next chunk on; the period starts at the sample count so far
    unmute()  zeroes for a further ``tail_ms`` (250 ms: audio already in the capture pipeline when the
              user pressed), then the period ends there
    finish()  at Stop: closes whatever is open at the total sample count. Stop always unmutes.

``mute()`` and ``unmute()`` come from the Tk thread, ``process()`` from the capture thread; one small
lock is held for a few instructions per chunk. Closed periods are collected for the recorder's ticker to
write to ``meta.json``, so no file I/O happens here.
"""

from __future__ import annotations

import threading

import numpy as np

from . import SAMPLE_RATE

TAIL_MS = 250

_OPEN, _MUTED, _TAIL = 0, 1, 2


class MicGate:
    def __init__(self, rate: int = SAMPLE_RATE, tail_ms: int = TAIL_MS, muted: bool = False):
        self.rate = rate
        self._tail = rate * tail_ms // 1000
        self._lock = threading.Lock()
        self._pos = 0  # mic samples seen so far
        self._state = _MUTED if muted else _OPEN
        self._start = 0  # first muted sample of the open period
        self._tail_end = 0  # while unmuting: the sample where zeroing stops
        self._closed: list[tuple[int, int]] = []  # (start, end) in samples, not yet taken

    # -- time ---------------------------------------------------------------------
    def _ms(self, samples: int) -> int:
        return samples * 1000 // self.rate

    # -- state ----------------------------------------------------------------------
    @property
    def muted(self) -> bool:
        """The user's setting: True from mute() until unmute() (not while the 250 ms tail runs out)."""
        return self._state == _MUTED

    @property
    def zeroing(self) -> bool:
        """Samples are being replaced right now (muted, or in the tail after unmute)."""
        return self._state != _OPEN

    @property
    def muted_since_ms(self) -> int | None:
        """Meeting time the open period started, None when no period is open (meta.json
        ``mic_muted_since_ms``; stays set through the tail, until the period has closed)."""
        with self._lock:
            return None if self._state == _OPEN else self._ms(self._start)

    @property
    def position_ms(self) -> int:
        with self._lock:
            return self._ms(self._pos)

    # -- control (any thread) -------------------------------------------------------------
    def mute(self) -> None:
        with self._lock:
            if self._state == _MUTED:
                return
            if self._state == _OPEN:
                self._start = self._pos
            # from the tail: the period simply carries on, no gap between the two
            self._state = _MUTED

    def unmute(self) -> None:
        with self._lock:
            if self._state == _MUTED:
                self._tail_end = self._pos + self._tail
                self._state = _TAIL

    def finish(self) -> None:
        """Stop: close an open period at the end of the audio and open the gate."""
        with self._lock:
            if self._state != _OPEN:
                self._closed.append((self._start, self._pos))
                self._state = _OPEN

    def take_closed(self) -> list[tuple[int, int]]:
        """Periods closed since the last call, as ``(start_ms, end_ms)`` pairs."""
        with self._lock:
            out, self._closed = self._closed, []
        return [(self._ms(a), self._ms(b)) for a, b in out if b > a]

    # -- capture thread ---------------------------------------------------------------------
    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        with self._lock:
            first = self._pos
            self._pos += n
            if self._state == _OPEN:
                return samples
            if self._state == _MUTED or first + n <= self._tail_end:
                return np.zeros(n, dtype=np.float32)
            # the tail ends inside this chunk: zero up to it, pass the rest, close the period
            cut = max(0, self._tail_end - first)
            self._closed.append((self._start, self._tail_end))
            self._state = _OPEN
            out = np.array(samples, dtype=np.float32)
            out[:cut] = 0.0
            return out
