"""Echo dedupe: drop mic segments that repeat what the speakers just played.

Without headphones the mic hears the call, which would duplicate remote
speech as "You". A mic segment is treated as echo when most of its words
also appear, in order, in system-stream text that overlaps it in time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .segments import Segment

_WORD = re.compile(r"[a-z0-9']+")


def normalize_words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def overlap_fraction(mic_words: list[str], sys_words: list[str]) -> float:
    """Fraction of mic words covered by in-order matches against system words."""
    if not mic_words or not sys_words:
        return 0.0
    matcher = SequenceMatcher(None, mic_words, sys_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(mic_words)


@dataclass(frozen=True)
class EchoMatch:
    """Why a mic segment counts as echo: the system segments it was compared with, and the score."""

    matched: list[Segment]  # system segments inside the window, in time order
    overlap: float  # overlap_fraction of the mic words against theirs


def match_echo(mic: Segment, system_segments: Iterable[Segment], window_ms: int = 1000,
               threshold: float = 0.6) -> EchoMatch | None:
    """The match that makes ``mic`` an echo of system speech, or None when it is not one."""
    nearby = [
        s for s in system_segments
        if s.start_ms <= mic.end_ms + window_ms and s.end_ms >= mic.start_ms - window_ms
    ]
    if not nearby:
        return None
    nearby.sort(key=lambda s: s.start_ms)
    sys_words: list[str] = []
    for s in nearby:
        sys_words.extend(normalize_words(s.text))
    mic_words = normalize_words(mic.text)
    if len(mic_words) <= 2:
        # Short replies ("yeah", "sounds good") are easy to match by accident: need all of them.
        threshold = 1.0
    overlap = overlap_fraction(mic_words, sys_words)
    return EchoMatch(nearby, overlap) if overlap >= threshold else None


def drop_event(mic: Segment, match: EchoMatch) -> dict:
    """The ``echo_dropped.jsonl`` line for a mic segment dropped as echo (docs/OUTPUT_SPEC.md)."""
    def line(s: Segment) -> dict:
        return {"start_ms": int(s.start_ms), "end_ms": int(s.end_ms), "text": s.text}

    return {"type": "echo_drop", **line(mic), "overlap": match.overlap, "matched": [line(s) for s in match.matched]}


def is_echo(mic: Segment, system_segments: Iterable[Segment], window_ms: int = 1000,
            threshold: float = 0.6) -> bool:
    return match_echo(mic, system_segments, window_ms, threshold) is not None
