"""On-device transcription with faster-whisper (F4).

English-only models, 8-bit weights, CPU only. Each utterance from the
segmenter is transcribed on its own, with word timings kept for the
after-Stop speaker pass.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from . import models
from .segments import Word

log = logging.getLogger(__name__)

# Phrases Whisper tends to invent on silence or noise.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "thanks for watching.",
    "you", "bye.", "subtitles by the amara.org community", ".", "...",
}


@dataclass
class Transcription:
    text: str
    confidence: float
    words: list[Word] = field(default_factory=list)  # ms relative to the utterance start


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray) -> Transcription: ...


class WhisperTranscriber:
    def __init__(self, model_name: str = "base.en", beam_size: int = 1, cpu_threads: int = 0,
                 models_root: Path | None = None):
        from faster_whisper import WhisperModel

        model_path = models.whisper_dir(model_name, models_root)
        if not models.whisper_installed(model_name, models_root):
            raise FileNotFoundError(f"Whisper model {model_name} is not installed at {model_path}")
        self.name = model_name
        self._beam = max(1, beam_size)
        self._model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                                   cpu_threads=cpu_threads, local_files_only=True)

    def transcribe(self, audio: np.ndarray) -> Transcription:
        segments, _info = self._model.transcribe(
            audio.astype(np.float32), language="en", beam_size=self._beam, vad_filter=False,
            word_timestamps=True, condition_on_previous_text=False, without_timestamps=False,
            temperature=[0.0, 0.2, 0.4],
        )
        texts: list[str] = []
        words: list[Word] = []
        logprob_sum = 0.0
        weight = 0.0
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            text = seg.text.strip()
            if not text:
                continue
            texts.append(text)
            dur = max(seg.end - seg.start, 0.1)
            logprob_sum += seg.avg_logprob * dur
            weight += dur
            for w in seg.words or []:
                if w.word.strip():
                    words.append(Word(int(w.start * 1000), int(w.end * 1000), w.word))
        text = _clean(" ".join(texts))
        if not text or text.lower() in _HALLUCINATIONS:
            return Transcription("", 0.0, [])
        confidence = math.exp(logprob_sum / weight) if weight else 0.0
        return Transcription(text, max(0.0, min(1.0, confidence)), words)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
