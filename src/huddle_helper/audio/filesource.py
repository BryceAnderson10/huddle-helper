"""Feed audio files through the recorder instead of live devices.

Used by tests, by the ``simulate`` command, and for re-processing recordings.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import SAMPLE_RATE
from .buffer import read_audio

OnAudio = Callable[[str, np.ndarray], None]


class FileCapture:
    sources = ("mic", "system")

    def __init__(self, on_audio: OnAudio, files: dict[str, Path | np.ndarray | None],
                 realtime: bool = False, chunk_ms: int = 100, **_ignored):
        self._on_audio = on_audio
        self._data: dict[str, np.ndarray] = {}
        for src in self.sources:
            item = files.get(src)
            if item is None:
                self._data[src] = np.zeros(0, dtype=np.float32)
            elif isinstance(item, np.ndarray):
                self._data[src] = item.astype(np.float32)
            else:
                self._data[src] = read_audio(Path(item))
        length = max(len(a) for a in self._data.values())
        for src, arr in self._data.items():  # equal length: both streams share one clock
            if len(arr) < length:
                self._data[src] = np.concatenate([arr, np.zeros(length - len(arr), np.float32)])
        self.length = length
        self._realtime = realtime
        self._chunk = SAMPLE_RATE * chunk_ms // 1000
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.finished = threading.Event()
        self.device_names = {"mic": "file", "system": "file"}

    def start(self, t0: float) -> None:
        self._thread = threading.Thread(target=self._run, args=(t0,), name="file-capture", daemon=True)
        self._thread.start()

    def _run(self, t0: float) -> None:
        pos = 0
        while pos < self.length and not self._stop.is_set():
            end = min(pos + self._chunk, self.length)
            for src in self.sources:
                self._on_audio(src, self._data[src][pos:end])
            pos = end
            if self._realtime:
                delay = t0 + pos / SAMPLE_RATE - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        self.finished.set()

    def stop(self) -> None:
        if self._thread is not None:
            self.finished.wait()
            self._thread.join()

    def abort(self) -> None:
        self._stop.set()
