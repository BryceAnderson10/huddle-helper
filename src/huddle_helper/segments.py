"""Transcript segment model shared by the pipeline, writer and finalizer."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCES = ("mic", "system")


@dataclass
class Word:
    start_ms: int
    end_ms: int
    text: str

    def to_list(self) -> list:
        return [self.start_ms, self.end_ms, self.text]

    @classmethod
    def from_list(cls, item: list) -> "Word":
        return cls(int(item[0]), int(item[1]), str(item[2]))


@dataclass
class Segment:
    source: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float
    speaker: str = ""
    words: list[Word] = field(default_factory=list)

    def to_event(self, seq: int) -> dict:
        return {
            "type": "segment",
            "seq": seq,
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "source": self.source,
            "speaker": self.speaker,
            "text": self.text,
            "confidence": round(float(self.confidence), 2),
        }

    @classmethod
    def from_event(cls, event: dict) -> "Segment":
        return cls(
            source=event["source"],
            start_ms=int(event["start_ms"]),
            end_ms=int(event["end_ms"]),
            text=event["text"],
            confidence=float(event.get("confidence", 0.0)),
            speaker=event.get("speaker", ""),
        )
