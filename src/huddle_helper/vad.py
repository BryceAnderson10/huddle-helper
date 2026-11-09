"""Streaming voice activity detection and utterance segmentation.

Uses the Silero VAD ONNX model bundled with faster-whisper, run frame by
frame (32 ms at 16 kHz) with its recurrent state carried across calls, and
cuts each stream into utterances at pauses so they can be transcribed and
written as soon as the speaker stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from .audio import SAMPLE_RATE

FRAME = 512  # samples per Silero frame at 16 kHz (32 ms)
CONTEXT = 64


class FrameScorer(Protocol):
    def __call__(self, frames: np.ndarray) -> np.ndarray:
        """Score a (n, FRAME) batch of consecutive frames; return n speech probabilities."""


class SileroScorer:
    """Stateful Silero VAD over consecutive frames of one stream."""

    def __init__(self):
        import os

        import onnxruntime
        from faster_whisper.utils import get_assets_path

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            os.path.join(get_assets_path(), "silero_vad_v6.onnx"),
            providers=["CPUExecutionProvider"], sess_options=opts,
        )
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        n = frames.shape[0]
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        prev = np.concatenate([self._context[None, :], frames[:-1, -CONTEXT:]], axis=0)
        batch = np.concatenate([prev, frames], axis=1).astype(np.float32)
        probs, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = frames[-1, -CONTEXT:].copy()
        return np.asarray(probs, dtype=np.float32).reshape(-1)


class EnergyScorer:
    """Tiny RMS-based scorer, for tests and as a fallback."""

    def __init__(self, threshold_rms: float = 0.02):
        self.threshold = threshold_rms

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        return np.clip(rms / (2 * self.threshold), 0.0, 1.0).astype(np.float32)


@dataclass
class Utterance:
    source: str
    start: int  # sample index on the meeting clock
    end: int
    audio: np.ndarray

    @property
    def start_ms(self) -> int:
        return self.start * 1000 // SAMPLE_RATE

    @property
    def end_ms(self) -> int:
        return self.end * 1000 // SAMPLE_RATE

    @property
    def duration_s(self) -> float:
        return (self.end - self.start) / SAMPLE_RATE


class UtteranceSegmenter:
    """Turns a contiguous stream of samples into utterances."""

    def __init__(self, source: str, on_utterance: Callable[[Utterance], None],
                 scorer: FrameScorer | None = None, threshold: float = 0.5,
                 min_silence_ms: int = 500, min_speech_ms: int = 250, speech_pad_ms: int = 200,
                 max_utterance_s: float = 12.0, split_search_s: float = 3.0):
        self.source = source
        self._emit = on_utterance
        self._scorer = scorer if scorer is not None else SileroScorer()
        self._on = threshold
        self._off = max(threshold - 0.15, 0.01)
        ms = SAMPLE_RATE // 1000
        self._min_silence = min_silence_ms * ms
        self._min_speech = min_speech_ms * ms
        self._pad = speech_pad_ms * ms
        self._max_len = int(max_utterance_s * SAMPLE_RATE)
        self._split_search = int(split_search_s * SAMPLE_RATE)

        self._pending = np.zeros(0, dtype=np.float32)  # samples not yet a full frame
        self._buf = np.zeros(0, dtype=np.float32)  # retained audio
        self._buf_start = 0  # sample index of _buf[0]
        self._pos = 0  # samples scored so far
        self._last_end = 0  # end of the last emitted utterance

        self._in_speech = False
        self._speech_start = 0
        self._silence_start: int | None = None
        self._frame_probs: list[tuple[int, float]] = []  # (frame start, prob) during speech

    # -- public ---------------------------------------------------------
    def feed(self, samples: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, samples.astype(np.float32, copy=False)])
        data = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        n_frames = len(data) // FRAME
        if n_frames:
            frames = data[: n_frames * FRAME].reshape(n_frames, FRAME)
            probs = self._scorer(frames)
            for i, p in enumerate(probs):
                self._step(self._pos + i * FRAME, float(p))
            self._pos += n_frames * FRAME
        self._pending = data[n_frames * FRAME:]
        self._trim()

    def flush(self) -> None:
        """End of stream: close any utterance in progress."""
        end = self._pos + len(self._pending)
        if self._in_speech:
            self._close(end)
        self._pos = end
        self._pending = np.zeros(0, dtype=np.float32)
        self._trim()

    @property
    def watermark(self) -> int:
        """No future utterance will start before this sample index."""
        if self._in_speech:
            return self._speech_start
        return max(self._last_end, self._pos - self._pad)

    @property
    def position(self) -> int:
        return self._pos + len(self._pending)

    # -- internals ------------------------------------------------------
    def _step(self, frame_start: int, prob: float) -> None:
        frame_end = frame_start + FRAME
        if not self._in_speech:
            if prob >= self._on:
                self._in_speech = True
                self._speech_start = max(frame_start - self._pad, self._last_end, self._buf_start)
                self._silence_start = None
                self._frame_probs = [(frame_start, prob)]
            return

        self._frame_probs.append((frame_start, prob))
        if prob < self._off:
            if self._silence_start is None:
                self._silence_start = frame_start
            if frame_end - self._silence_start >= self._min_silence:
                self._close(min(self._silence_start + self._pad, frame_end))
                return
        else:
            self._silence_start = None

        if frame_end - self._speech_start >= self._max_len:
            self._split(frame_end)

    def _split(self, frame_end: int) -> None:
        """Utterance too long: cut at the least speech-like frame near the end."""
        lo = frame_end - self._split_search
        # Lowest speech probability wins; among ties, the latest frame (longest first piece).
        candidates = [(p, -s) for s, p in self._frame_probs if s >= lo]
        cut = -min(candidates)[1] + FRAME // 2 if candidates else frame_end
        cut = max(cut, self._speech_start + self._min_speech)
        piece = [(s, p) for s, p in self._frame_probs if s < cut]
        self._frame_probs = [(s, p) for s, p in self._frame_probs if s >= cut]
        self._emit_range(self._speech_start, cut, piece)
        self._speech_start = cut
        if self._silence_start is not None and self._silence_start < cut:
            self._silence_start = cut

    def _close(self, end: int) -> None:
        probs = self._frame_probs
        self._in_speech = False
        self._silence_start = None
        self._frame_probs = []
        self._emit_range(self._speech_start, end, probs)

    def _emit_range(self, start: int, end: int, probs: list[tuple[int, float]]) -> None:
        self._last_end = max(self._last_end, end)
        speech_samples = sum(FRAME for _, p in probs if p >= self._off)
        if end - start < self._min_speech or speech_samples < self._min_speech:
            return
        a = start - self._buf_start
        b = end - self._buf_start
        audio = self._buf[max(a, 0):max(b, 0)].copy()
        if len(audio) == 0:
            return
        self._emit(Utterance(self.source, start, end, audio))

    def _trim(self) -> None:
        """Drop audio that can no longer be part of an utterance."""
        keep_from = self._speech_start if self._in_speech else self._pos - self._pad
        keep_from = max(keep_from, self._buf_start)
        drop = keep_from - self._buf_start
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start = keep_from
