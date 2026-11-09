"""Compressed on-disk audio buffer for each stream (F10)."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from . import SAMPLE_RATE


class FlacBuffer:
    """Appends 16 kHz mono audio to a FLAC file as it is captured."""

    def __init__(self, path: Path, rate: int = SAMPLE_RATE):
        import soundfile as sf

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = sf.SoundFile(str(self.path), "w", samplerate=rate, channels=1,
                                  format="FLAC", subtype="PCM_16")
        self._lock = threading.Lock()
        self.frames = 0

    def write(self, samples: np.ndarray) -> None:
        with self._lock:
            if self._file.closed:
                return
            self._file.write(np.clip(samples, -1.0, 1.0))
            self.frames += len(samples)

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


def read_audio(path: Path) -> np.ndarray:
    """Read a buffered stream back as float32 mono at 16 kHz."""
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != SAMPLE_RATE:
        import soxr

        data = soxr.resample(data, rate, SAMPLE_RATE).astype(np.float32)
    return data
