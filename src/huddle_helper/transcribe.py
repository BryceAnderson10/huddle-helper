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
from typing import Callable, Iterable, Protocol

import numpy as np

from . import models
from .audio import SAMPLE_RATE
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

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> Transcription: ...


class WhisperTranscriber:
    def __init__(self, model_name: str = "base.en", beam_size: int = 1, cpu_threads: int = 0,
                 models_root: Path | None = None, trim_always: bool = False):
        """``trim_always`` (the second pass after Stop, WP15) drops invented later segments on every
        call, not only prompted ones; see ``read_segments``."""
        from faster_whisper import WhisperModel

        model_path = models.whisper_dir(model_name, models_root)
        if not models.whisper_installed(model_name, models_root):
            raise FileNotFoundError(f"Whisper model {model_name} is not installed at {model_path}")
        self.name = model_name
        self._beam = max(1, beam_size)
        self._trim_always = trim_always
        self._model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                                   cpu_threads=cpu_threads, local_files_only=True)

    def count_tokens(self, text: str) -> int:
        """Whisper tokens in ``text``, for the vocabulary budget."""
        return len(self._model.hf_tokenizer.encode(text, add_special_tokens=False).ids)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> Transcription:
        """``prompt`` (the vocabulary sentence) goes to Whisper as ``hotwords``. Only then are
        the segments cut short and trimmed (see ``read_segments``); without one the output is
        exactly what Whisper returns."""
        prompt = prompt or None
        extra = {"hotwords": prompt} if prompt else {}
        segments, _info = self._model.transcribe(
            normalize_level(audio), language="en", beam_size=self._beam, vad_filter=False,
            word_timestamps=True, condition_on_previous_text=False, without_timestamps=False,
            temperature=[0.0, 0.2, 0.4], **extra,
        )
        if prompt:
            segments = read_segments(segments, len(audio) / SAMPLE_RATE)
        elif getattr(self, "_trim_always", False):
            segments = read_segments(segments, len(audio) / SAMPLE_RATE, stop=False)
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
        if words:
            words = drop_hallucinated(words)
            text = _clean("".join(w.text for w in words))
        if not text or text.lower() in _HALLUCINATIONS:
            return Transcription("", 0.0, [])
        confidence = math.exp(logprob_sum / weight) if weight else 0.0
        return Transcription(text, max(0.0, min(1.0, confidence)), words)


def final_transcriber_factory(settings, threads: Callable[[], int]) -> Callable[[], "WhisperTranscriber"] | None:
    """The second-pass transcriber for the final transcript (WP15), or None when it is off or its
    model is not installed (the final pass then keeps the live text). ``threads`` is read when the
    model loads, so a recording started meanwhile gets the smaller number."""
    if not getattr(settings, "final_transcribe", False):
        return None
    name = settings.final_model
    if not models.whisper_installed(name):
        log.warning("Final-transcript model %s is not installed; keeping the live text", name)
        return None
    return lambda: WhisperTranscriber(name, settings.final_beam_size, threads(), trim_always=True)


# With a prompt, Whisper decodes one more window over the 0.2 s of padding after the last
# word and invents text there ("Mm.", "SOT", echoes of the list), and the low score triggers
# temperature retries, 3.6x slower overall. So stop reading segments once one ends this close
# to the end of the audio. Without a prompt the extra window comes back empty and cheap, and
# stopping would cut real last words, so it is not applied. Values: TODO.md, "Transcription
# quality" (stop 0.7, chosen over 1.0 and 0.5).
PROMPT_STOP_S = 0.7
# Later segments (after the first kept one) that look like that junk are dropped. Real later
# segments sit at avg_logprob -0.77 or better and no-speech 0.06 or less; junk at -0.9 or
# worse and 0.2 or more. Same TODO.md section.
LATE_MIN_AVG_LOGPROB = -0.8
LATE_MAX_COMPRESSION_RATIO = 2.4
LATE_MAX_NO_SPEECH_PROB = 0.2


def is_skipped(seg) -> bool:
    """Segments the transcriber ignores: confident non-speech, or no text."""
    return (seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0) or not seg.text.strip()


def read_segments(segments: Iterable, audio_s: float, stop: bool = True) -> list:
    """Read Whisper's (lazy) segments for a prompted call: stop after a segment that ends
    within PROMPT_STOP_S of the end of the audio, so the leftover window is never decoded, and
    drop later segments that look like invented text. Pure; works on any objects with
    start/end/text/avg_logprob/compression_ratio/no_speech_prob. ``stop=False`` keeps reading to
    the end and only drops (an unprompted call: stopping could cut a real last word)."""
    kept: list = []
    seen = False  # a real segment has been kept
    for seg in segments:
        if is_skipped(seg):
            kept.append(seg)  # the caller ignores these
        elif seen and (seg.avg_logprob < LATE_MIN_AVG_LOGPROB
                       or seg.compression_ratio > LATE_MAX_COMPRESSION_RATIO
                       or seg.no_speech_prob > LATE_MAX_NO_SPEECH_PROB):
            pass
        else:
            seen = True
            kept.append(seg)
        if stop and audio_s - seg.end < PROMPT_STOP_S:
            break
    return kept


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


TARGET_PEAK = 0.5
MAX_GAIN = 30.0  # about +30 dB; louder boosts mostly amplify noise


def normalize_level(audio: np.ndarray) -> np.ndarray:
    """Boost quiet audio toward a target peak before transcription.

    Test #2's microphone peaked at 0.047; Whisper is less reliable (and hallucinated a
    repetition loop) on audio that quiet. Loud audio is left untouched.
    """
    audio = audio.astype(np.float32)
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if peak <= 0 or peak >= TARGET_PEAK:
        return audio
    return audio * min(TARGET_PEAK / peak, MAX_GAIN)


MIN_MEAN_WORD_MS = 60  # real speech is ~150-400 ms per word; hallucinated words are ~0 ms
MAX_REPEATS = 2  # "I found it. I found it." is real; a third identical sentence is a loop


def _sentences(words: list[Word]) -> list[list[Word]]:
    out: list[list[Word]] = [[]]
    for w in words:
        out[-1].append(w)
        if w.text.strip().endswith((".", "?", "!")):
            out.append([])
    return [s for s in out if s]


def drop_hallucinated(words: list[Word]) -> list[Word]:
    """Remove Whisper loops: sentences squeezed into near-zero time, and runaway repeats.

    In Test #2 a quiet mic line ended with "Alright, let's go check this one." five times,
    every word 0-20 ms long and crammed into the last second of the audio.
    """
    kept: list[list[Word]] = []
    repeats = 0
    for sent in _sentences(words):
        if len(sent) >= 2:
            mean_ms = sum(w.end_ms - w.start_ms for w in sent) / len(sent)
            if mean_ms < MIN_MEAN_WORD_MS:
                log.debug("Dropped implausibly fast sentence: %r", "".join(w.text for w in sent))
                continue
        key = _clean(" ".join(w.text for w in sent)).lower().strip(".?!")
        prev = _clean(" ".join(w.text for w in kept[-1])).lower().strip(".?!") if kept else None
        repeats = repeats + 1 if key == prev else 1
        if repeats > MAX_REPEATS:
            continue
        kept.append(sent)
    return [w for sent in kept for w in sent]
