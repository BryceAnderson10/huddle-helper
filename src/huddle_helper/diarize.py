"""Speaker labels (F5, F8).

Live pass: a voice fingerprint (speaker embedding) per utterance, grouped by
cosine similarity into provisional speakers.

Refined pass (after Stop): full offline diarization over the buffered audio
(pyannote segmentation + speaker embeddings + clustering, via sherpa-onnx).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from . import models
from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

SYSTEM_PREFIX = "SPEAKER_"
MIC_PREFIX = "MIC_SPEAKER_"
YOU = "You"
REMOTE = "Remote"


def label(prefix: str, n: int) -> str:
    return f"{prefix}{n:02d}"


class Embedder(Protocol):
    def embed(self, audio: np.ndarray) -> np.ndarray | None: ...


class SherpaEmbedder:
    def __init__(self, models_root: Path | None = None, num_threads: int = 1):
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models.embedding_model(models_root)), num_threads=num_threads)
        if not cfg.validate():
            raise FileNotFoundError("Speaker embedding model is not installed")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        if len(audio) < SAMPLE_RATE // 2:
            return None
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        vec = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else None


class OnlineClusterer:
    """Incremental speaker grouping for provisional live labels."""

    def __init__(self, prefix: str, threshold: float = 0.75, min_new_speaker_s: float = 1.5):
        self.prefix = prefix
        self.threshold = threshold
        self.min_new_speaker_s = min_new_speaker_s
        self._centroids: list[np.ndarray | None] = []  # None: speaker seen without a fingerprint yet
        self._last: int | None = None

    def assign(self, embedding: np.ndarray | None, duration_s: float) -> str:
        weight = max(duration_s, 0.1)
        if embedding is None or not len(embedding):
            if self._last is None:
                self._new(None)
            return label(self.prefix, self._last + 1)

        scored = [(i, float(np.dot(embedding, c / (np.linalg.norm(c) or 1.0))))
                  for i, c in enumerate(self._centroids) if c is not None]
        if not scored:
            if self._centroids:  # fill the placeholder speaker
                self._centroids[0] = embedding * weight
                self._last = 0
                return label(self.prefix, 1)
            return label(self.prefix, self._new(embedding * weight) + 1)

        idx, sim = max(scored, key=lambda item: item[1])
        if sim >= self.threshold:
            # Confident match: refine that speaker's fingerprint.
            self._centroids[idx] = self._centroids[idx] + embedding * weight
        elif duration_s >= self.min_new_speaker_s:
            return label(self.prefix, self._new(embedding * weight) + 1)
        # Otherwise too short to trust as a new voice: attach to the closest match
        # without letting it blur that speaker's fingerprint.
        self._last = idx
        return label(self.prefix, idx + 1)

    def _new(self, centroid: np.ndarray | None) -> int:
        self._centroids.append(centroid)
        self._last = len(self._centroids) - 1
        return self._last

    @property
    def count(self) -> int:
        return len(self._centroids)


@dataclass
class Turn:
    start_ms: int
    end_ms: int
    speaker: int


class OfflineDiarizer(Protocol):
    def diarize(self, audio: np.ndarray, progress: Callable[[float], None] | None = None) -> list[Turn]: ...


def merge_small_speakers(turns: list[Turn], centroids: dict[int, np.ndarray], min_s: float = 15.0,
                         min_fraction: float = 0.05, min_similarity: float = 0.7) -> list[Turn]:
    """Fold speakers with little speech into the most similar larger speaker.

    Offline clustering tends to spin off small clusters from a main voice (Test #2: three
    "speakers" with 2-10 s each, at 0.83-0.90 similarity to a main speaker). A small
    cluster is merged only if it really sounds like a larger one, so a genuinely
    different person who speaks briefly keeps their own label.
    """
    totals: dict[int, float] = {}
    for t in turns:
        totals[t.speaker] = totals.get(t.speaker, 0.0) + (t.end_ms - t.start_ms) / 1000
    speech = sum(totals.values())
    if not speech:
        return turns
    limit = max(min_s, min_fraction * speech)
    small = sorted((k for k, v in totals.items() if v < limit), key=totals.get)
    large = [k for k in totals if k not in small and k in centroids]
    remap: dict[int, int] = {}
    for k in small:
        if k not in centroids or not large:
            continue
        best = max(large, key=lambda j: float(np.dot(centroids[k], centroids[j])))
        if float(np.dot(centroids[k], centroids[best])) >= min_similarity:
            remap[k] = best
            log.info("Merged speaker %d (%.0f s) into speaker %d", k, totals[k], best)
    if not remap:
        return turns
    merged = [Turn(t.start_ms, t.end_ms, remap.get(t.speaker, t.speaker)) for t in turns]
    # Join back-to-back turns that now share a speaker.
    out: list[Turn] = []
    for t in merged:
        if out and out[-1].speaker == t.speaker and t.start_ms <= out[-1].end_ms + 50:
            out[-1] = Turn(out[-1].start_ms, max(out[-1].end_ms, t.end_ms), t.speaker)
        else:
            out.append(t)
    return out


def speaker_centroids(audio: np.ndarray, turns: list[Turn], embedder: Embedder) -> dict[int, np.ndarray]:
    """Average voice fingerprint per speaker, from that speaker's turns."""
    by_speaker: dict[int, list[np.ndarray]] = {}
    for t in turns:
        vec = embedder.embed(audio[t.start_ms * SAMPLE_RATE // 1000:t.end_ms * SAMPLE_RATE // 1000])
        if vec is not None:
            by_speaker.setdefault(t.speaker, []).append(vec)
    out = {}
    for k, vecs in by_speaker.items():
        c = np.mean(vecs, axis=0)
        n = np.linalg.norm(c)
        if n > 0:
            out[k] = c / n
    return out


class SherpaOfflineDiarizer:
    def __init__(self, models_root: Path | None = None, threshold: float = 0.6, num_threads: int = 2,
                 merge_small: bool = True):
        import sherpa_onnx

        self._models_root = models_root
        self._merge_small = merge_small

        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(models.segmentation_model(models_root))),
                num_threads=num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(models.embedding_model(models_root)), num_threads=num_threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not cfg.validate():
            raise FileNotFoundError("Diarization models are not installed")
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def diarize(self, audio: np.ndarray, progress: Callable[[float], None] | None = None) -> list[Turn]:
        if len(audio) < SAMPLE_RATE:
            return []

        def cb(done: int, total: int) -> int:
            if progress and total:
                progress(done / total)
            return 0

        result = self._sd.process(audio.astype(np.float32), callback=cb).sort_by_start_time()
        turns = [Turn(int(r.start * 1000), int(r.end * 1000), int(r.speaker)) for r in result]
        if self._merge_small and len({t.speaker for t in turns}) > 1:
            centroids = speaker_centroids(audio, turns, SherpaEmbedder(self._models_root))
            turns = merge_small_speakers(turns, centroids)
        return turns
