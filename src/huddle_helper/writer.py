"""JSONL writer and the time-ordered merge of both streams (F6).

Each stream reports a *watermark*: a time (ms from meeting start) before which
it will never produce another segment. The merger releases pending segments in
start-time order once every stream's watermark has passed them, so lines in
transcript.jsonl are ordered by start time across both streams, and each line
is written and flushed as soon as that is safe.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Iterable

from . import SCHEMA_VERSION
from .echo import is_echo
from .segments import Segment

log = logging.getLogger(__name__)


def dumps(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


class JsonlWriter:
    """Append-only JSONL file; every line is flushed and fsynced on write."""

    def __init__(self, path: Path, mode: str = "a"):
        self.path = Path(path)
        self._fh = open(self.path, mode, encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, event: dict) -> None:
        line = dumps(event) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def meeting_start_event(meeting_id: str, started_at: str) -> dict:
    return {"type": "meeting_start", "meeting_id": meeting_id, "started_at": started_at,
            "schema": SCHEMA_VERSION}


def meeting_end_event(ended_at: str, segments: int, **extra) -> dict:
    return {"type": "meeting_end", "ended_at": ended_at, "segments": segments, **extra}


def write_transcript(path: Path, meeting_id: str, started_at: str, ended_at: str,
                     segments: Iterable[Segment], **end_extra) -> int:
    """Write a complete transcript file (used for transcript.final.jsonl)."""
    count = 0
    with JsonlWriter(path, mode="w") as w:
        w.write(meeting_start_event(meeting_id, started_at))
        for seg in segments:
            count += 1
            w.write(seg.to_event(count))
        w.write(meeting_end_event(ended_at, count, **end_extra))
    return count


def read_events(path: Path) -> list[dict]:
    """Read a JSONL transcript, ignoring a trailing partial line (crash safety)."""
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                log.warning("Skipping unreadable line in %s", path)
    return events


class SegmentMerger:
    """Merge segments from several streams into one ordered, deduped sequence."""

    def __init__(self, sources: Iterable[str], sink: Callable[[Segment], None],
                 echo_dedupe: bool = True, echo_window_ms: int = 1000,
                 history_ms: int = 60_000):
        self._sources = list(sources)
        self._watermarks = {s: 0 for s in self._sources}
        self._pending: list[Segment] = []
        self._recent_system: list[Segment] = []
        self._sink = sink
        self._echo = echo_dedupe and "mic" in self._sources and "system" in self._sources
        self._echo_window = echo_window_ms
        self._history_ms = history_ms
        self._lock = threading.Lock()
        self.dropped_echo = 0

    def update_watermark(self, source: str, ms: int) -> None:
        with self._lock:
            if ms > self._watermarks[source]:
                self._watermarks[source] = ms
            self._release(final=False)

    def add(self, segment: Segment) -> None:
        with self._lock:
            self._pending.append(segment)
            if segment.source == "system":
                self._recent_system.append(segment)
            self._release(final=False)

    def flush_all(self) -> None:
        with self._lock:
            self._release(final=True)

    def min_watermark(self) -> int:
        with self._lock:
            return min(self._watermarks.values())

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # -- internals ------------------------------------------------------
    def _eligible(self, seg: Segment, final: bool) -> bool:
        if final:
            return True
        if seg.start_ms >= min(self._watermarks.values()):
            return False
        if self._echo and seg.source == "mic":
            # Every system segment that could overlap this one must be known.
            return self._watermarks["system"] >= seg.end_ms + self._echo_window
        return True

    def _release(self, final: bool) -> None:
        self._pending.sort(key=lambda s: (s.start_ms, s.source))
        while self._pending and self._eligible(self._pending[0], final):
            seg = self._pending.pop(0)
            if self._echo and seg.source == "mic" and is_echo(seg, self._recent_system, self._echo_window):
                self.dropped_echo += 1
                log.debug("Dropped echo mic segment at %d ms: %r", seg.start_ms, seg.text)
                continue
            self._sink(seg)
        if self._recent_system:
            horizon = min(self._watermarks.values()) - self._history_ms
            self._recent_system = [s for s in self._recent_system if s.end_ms >= horizon]
