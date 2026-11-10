"""The last minutes of one stream, in memory, 16-bit (plan 4.7, LIVE_VIEW "Click to hear").

``RecentAudio.write`` is called by the stream worker for the audio that has *already passed the mic
gate* (``Recorder._on_audio`` zeroes muted samples first), so the ring holds exactly what ``mic.flac``
holds: muted speech can never be played back. It runs on the stream worker thread, not in the capture
callback: one int16 conversion and a copy per chunk, about 19 MB per stream for 10 minutes.

Positions are sample counts on the meeting clock (the aligner pads each stream, so sample ``n`` is
``n / 16`` ms). ``read`` returns None for a range that has left the window, never partial audio.
"""

from __future__ import annotations

import threading

import numpy as np

from . import SAMPLE_RATE


class RecentAudio:
    def __init__(self, seconds: float = 600.0, rate: int = SAMPLE_RATE):
        self.rate = rate
        self.capacity = max(1, int(seconds * rate))
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._total = 0  # samples written since the start (the head)
        self._lock = threading.Lock()

    # -- positions ---------------------------------------------------------------
    def _ms(self, samples: int) -> int:
        return samples * 1000 // self.rate

    @property
    def total_ms(self) -> int:
        """The end of the audio written so far, on the meeting clock."""
        with self._lock:
            return self._ms(self._total)

    @property
    def oldest_ms(self) -> int:
        """The earliest meeting time still held (0 until the ring has wrapped). Rounded up, so a range
        starting at ``oldest_ms`` is always inside the window."""
        with self._lock:
            first = max(0, self._total - self.capacity)
            return -(-first * 1000 // self.rate)

    # -- writing (stream worker) ----------------------------------------------------------
    def write(self, samples: np.ndarray) -> None:
        n = len(samples)
        if not n:
            return
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        with self._lock:
            if n >= self.capacity:  # a chunk bigger than the ring: keep its tail
                self._buf[:] = pcm[-self.capacity:]
            else:
                pos = self._total % self.capacity
                head = min(n, self.capacity - pos)
                self._buf[pos:pos + head] = pcm[:head]
                if head < n:  # wrapped
                    self._buf[:n - head] = pcm[head:]
            self._total += n

    # -- reading (any thread) ---------------------------------------------------------------
    def read(self, start_ms: int, end_ms: int) -> np.ndarray | None:
        """``start_ms .. end_ms`` as float32. ``end_ms`` is clamped to what has been written; None when
        ``start_ms`` has left the window (or is not written yet) or the range is empty."""
        a = int(start_ms) * self.rate // 1000
        b = int(end_ms) * self.rate // 1000
        with self._lock:
            b = min(b, self._total)
            if a < 0 or a < self._total - self.capacity or b <= a:
                return None
            idx = np.arange(a, b) % self.capacity
            pcm = self._buf[idx]
        return pcm.astype(np.float32) / 32767.0
