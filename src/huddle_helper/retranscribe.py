"""Transcribe the kept audio a second time after Stop (WP15).

The live pass has to keep up with the meeting, so it runs a small model. After Stop there is time
for a stronger one. Each live line's stretch of audio is transcribed again and the line keeps its
place: same source, same start and end, one line in and one line out, so names, bookmarks and
click-to-hear still point at it. Only its text, confidence and word timings change.

UI-free and model-free: the transcriber is anything with ``transcribe(audio, prompt)`` that returns
a ``transcribe.Transcription`` (text, confidence, words in ms from the start of the audio given).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import numpy as np

from .audio import SAMPLE_RATE
from .segments import Segment, Word
from .transcribe import Transcription

log = logging.getLogger(__name__)

# Audio given to the model around a line: Whisper's word times run a little short at both ends
# (clips needed up to 660 ms after the last word, TODO.md "Clip edges"). The lab runs used these.
PRE_MS = 200
POST_MS = 300
# Under this much audio a model has nothing to work with; the live line is kept.
MIN_SPAN_MS = 300


class FinalTranscriber(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> Transcription: ...


@dataclass
class RetxStats:
    lines: int = 0  # lines looked at
    changed: int = 0  # lines whose words changed
    kept_live: int = 0  # lines where the new text was refused (see ``suspect``) or the call failed
    no_audio: int = 0  # lines of a source with no kept audio, or too short
    seconds: float = 0.0


def _n_words(text: str) -> int:
    return len(re.findall(r"[\w']+", text))


def _norm(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def span(seg: Segment, prev_end_ms: int | None, next_start_ms: int | None, audio_ms: int) -> tuple[int, int]:
    """The stretch of audio for a line: PRE_MS before to POST_MS after, never reaching into the
    neighbouring lines of the same source (their words belong to them)."""
    a = seg.start_ms - PRE_MS
    if prev_end_ms is not None:
        a = max(a, min(prev_end_ms, seg.start_ms))
    b = seg.end_ms + POST_MS
    if next_start_ms is not None:
        b = min(b, max(next_start_ms, seg.end_ms))
    return max(0, a), min(audio_ms, b)


def suspect(live_text: str, new_text: str) -> str | None:
    """Why the new text is refused, or None when it is taken.

    A second pass can fail in ways the live pass did not: come back empty, drop most of a line, or
    run on into invented text. Those are caught by length alone. A wrong word of the same length
    is not catchable here and is accepted (the reference transcript measures the net effect).
    """
    n_live, n_new = _n_words(live_text), _n_words(new_text)
    if n_new == 0:
        return "empty"
    if n_new * 2 < n_live:
        return "under half the words"
    if n_new > max(2 * n_live, n_live + 2):
        return "over twice the words"
    return None


def retranscribe(segments: list[Segment], words: dict[tuple[str, int], list[Word]],
                 audio_by_source: dict[str, np.ndarray], transcriber: FinalTranscriber,
                 prompt: str | None = None, progress: Callable[[float], None] | None = None,
                 ) -> tuple[list[Segment], dict[tuple[str, int], list[Word]], RetxStats]:
    """New segments (same order, sources, starts and ends) and word timings, with what happened.

    ``words`` is keyed as ``finalize.load_words`` returns it, by (source, the line's start). Lines
    whose new text is refused, whose source has no audio, or whose call fails keep their live text
    and words untouched. Nothing here raises for a single bad line.
    """
    stats = RetxStats()
    t0 = time.perf_counter()
    out = list(segments)
    new_words = dict(words)
    by_source: dict[str, list[int]] = {}
    for i, seg in enumerate(segments):
        by_source.setdefault(seg.source, []).append(i)
    total = len(segments) or 1
    done = 0
    for source, idxs in by_source.items():
        audio = audio_by_source.get(source)
        idxs = sorted(idxs, key=lambda i: segments[i].start_ms)
        for k, i in enumerate(idxs):
            seg = segments[i]
            stats.lines += 1
            done += 1
            prev_end = segments[idxs[k - 1]].end_ms if k else None
            next_start = segments[idxs[k + 1]].start_ms if k + 1 < len(idxs) else None
            _redo_line(seg, i, prev_end, next_start, audio, transcriber, prompt, out, new_words, stats)
            if progress:
                progress(done / total)  # every line moves the bar, kept ones too
    stats.seconds = time.perf_counter() - t0
    return out, new_words, stats


def _redo_line(seg: Segment, i: int, prev_end: int | None, next_start: int | None,
               audio: np.ndarray | None, transcriber: FinalTranscriber, prompt: str | None,
               out: list[Segment], new_words: dict[tuple[str, int], list[Word]], stats: RetxStats) -> None:
    """Transcribe one line again and, unless the new text is refused, put it in ``out[i]``."""
    if audio is None:
        stats.no_audio += 1
        return
    a, b = span(seg, prev_end, next_start, len(audio) * 1000 // SAMPLE_RATE)
    if b - a < MIN_SPAN_MS:
        stats.no_audio += 1
        return
    try:
        result = transcriber.transcribe(audio[a * SAMPLE_RATE // 1000:b * SAMPLE_RATE // 1000], prompt)
    except Exception:  # noqa: BLE001 - one bad line must not cost the pass
        log.exception("Second transcription failed for the %s line at %d ms", seg.source, seg.start_ms)
        stats.kept_live += 1
        return
    why = suspect(seg.text, result.text)
    if why is not None:
        log.info("Kept the live text of the %s line at %d ms: new text %s", seg.source, seg.start_ms, why)
        stats.kept_live += 1
        return
    if _norm(result.text) != _norm(seg.text):
        stats.changed += 1
    out[i] = replace(seg, text=result.text, confidence=result.confidence)
    key = (seg.source, seg.start_ms)
    if result.words:
        new_words[key] = [Word(a + w.start_ms, a + w.end_ms, w.text) for w in result.words]
    else:
        # The live words spell the live text; the final pass would rebuild the line from them.
        new_words.pop(key, None)
