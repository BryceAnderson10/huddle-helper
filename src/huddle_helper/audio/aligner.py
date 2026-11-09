"""Keep each stream on the shared meeting clock.

WASAPI loopback delivers no packets while nothing is playing, and devices can
drop out briefly. The aligner turns "chunks that arrived at time t" into one
contiguous 16 kHz stream whose sample index equals milliseconds since the
meeting started, by inserting silence for any gap. A periodic tick pads
silence for a stream that has gone quiet, so downstream stages keep moving.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import SAMPLE_RATE

OnSamples = Callable[[np.ndarray], None]


class StreamAligner:
    def __init__(self, t0: float, on_samples: OnSamples, rate: int = SAMPLE_RATE,
                 gap_threshold_s: float = 0.2, idle_latency_s: float = 0.5):
        self.t0 = t0
        self.rate = rate
        self.pos = 0  # samples emitted so far
        self.padded = 0  # samples of inserted silence
        self._on_samples = on_samples
        self._gap = int(gap_threshold_s * rate)
        self._idle_latency = int(idle_latency_s * rate)

    def _expected(self, t: float) -> int:
        return int(round((t - self.t0) * self.rate))

    def _pad(self, n: int) -> None:
        if n > 0:
            self._on_samples(np.zeros(n, dtype=np.float32))
            self.pos += n
            self.padded += n

    def push(self, t: float, samples: np.ndarray) -> None:
        """Add samples whose last sample was captured at about time ``t``."""
        n = len(samples)
        if n == 0:
            return
        missing = self._expected(t) - n - self.pos
        if missing > self._gap:
            self._pad(missing)
        self._on_samples(samples.astype(np.float32, copy=False))
        self.pos += n

    def tick(self, now: float) -> None:
        """Pad silence if the stream has delivered nothing for a while."""
        behind = self._expected(now) - self._idle_latency - self.pos
        if behind > 0:
            self._pad(behind)

    def finish(self, now: float) -> None:
        """Pad silence right up to ``now`` (used at Stop so streams end together)."""
        self._pad(self._expected(now) - self.pos)

    @property
    def position_ms(self) -> int:
        return self.pos * 1000 // self.rate
