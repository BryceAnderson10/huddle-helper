"""Speaker labels (F5, F8).

Live pass: a voice fingerprint (speaker embedding) per utterance, grouped by
cosine similarity into provisional speakers.

Refined pass (after Stop): full offline diarization over the buffered audio
(pyannote segmentation + speaker embeddings + clustering, via sherpa-onnx).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
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
# Audio shorter than this gets no voice fingerprint, so it cannot steer clustering.
MIN_FINGERPRINT_S = 0.5


def label(prefix: str, n: int) -> str:
    return f"{prefix}{n:02d}"


def final_threads(recording: bool = False, cpu_count: int | None = None) -> int:
    """Threads for the after-Stop embedder and diarizer: half the cores up to 4, 1 while recording."""
    if recording:
        return 1
    return min(4, max(1, (cpu_count or os.cpu_count() or 2) // 2))


@dataclass(frozen=True)
class VoiceProfile:
    """The similarity cutoffs that belong to one fingerprint model.

    Every model scores "how alike are these two voices" on its own scale, so the cutoffs travel
    with the model and not with the user's settings. Each value is the centre of the range that
    passed on the kept recordings (TODO.md, "Speakers: other fingerprint models"); the range is
    in the comment beside it.
    """
    model: str  # file stem, as models.EMBEDDING_NAME
    live_match: float  # OnlineClusterer: at least this alike is the same live speaker
    live_adopt: float  # OnlineClusterer: a weakly known speaker adopts a long line this alike
    final_match: float  # cluster_voices: clusters at least this alike are merged
    fold_min: float  # fold_small_clusters: a small cluster folds only into one this alike
    tag_gate: float  # tags.piece_names: a speaker-level name steers a piece only if this alike
    split_cluster: float  # sherpa's offline clustering, which only decides where lines split
    # Recognising You inside a shared mic stretch (finalize.you_decision, #56). Not fitted yet: chosen
    # well above final_match and on the side of not relabelling (docs/QUALITY_PLAN.md).
    you_min_s: float  # seconds of fingerprinted mic speech outside every stretch, at least
    you_match: float  # the best mic speaker's voice is at least this like that speech
    you_lead: float  # ...and at least this much more alike than the runner-up


SHIPPED_UNTIL_WP13 = "wespeaker_en_voxceleb_resnet34_LM"
PROFILES: dict[str, VoiceProfile] = {
    # The model shipped until WP13. final_match passed only from 0.70 to 0.71 (WP1: 0.685-0.71).
    SHIPPED_UNTIL_WP13: VoiceProfile(SHIPPED_UNTIL_WP13, live_match=0.75, live_adopt=0.6, final_match=0.70,
                                     fold_min=0.70, tag_gate=0.70, split_cluster=0.6,
                                     # same-speaker line pairs p50 0.77, different p90 0.71 (TODO.md)
                                     you_min_s=30.0, you_match=0.85, you_lead=0.10),
    # 3D-Speaker ERes2Net, English. final_match: on the pieces the final pass really makes, confirmed
    # speech has 0 s wrong from 0.27 to 0.47 (Tests #3-#5), and every recording keeps the same
    # speakers from 0.34 to 0.47 (below 0.34 speakers nobody has confirmed start to merge; from 0.48
    # the two halves of Test #5's 5 s speaker stop joining and are folded away by the under-4 s rule).
    # 0.40 is the centre of that. On fixed pieces the range ran to 0.60. live_match from 0.35 to 0.55 (replay of Tests #2-#5, tools/live_lab.py), adopt had no effect
    # from 0.25 to 0.40. split_cluster: 0.6 to 0.9 all give the old model's number of pieces within
    # 10%; 0.9 gives the fewest invented speakers (sherpa's value is a distance: higher means fewer).
    # you_match: above the top of the range that passed on fixed pieces (0.60), so a voice that only
    # just merges with the user's is never called You; you_lead: wider than final_match's whole 0.13-wide
    # working range, so of two mic speakers clustering only just kept apart neither is called You.
    "3dspeaker_speech_eres2net_sv_en_voxceleb_16k": VoiceProfile(
        "3dspeaker_speech_eres2net_sv_en_voxceleb_16k", live_match=0.42, live_adopt=0.30, final_match=0.40,
        fold_min=0.40, tag_gate=0.40, split_cluster=0.9, you_min_s=30.0, you_match=0.65, you_lead=0.20),
}


def profile(model: str | None = None, **overrides: float | None) -> VoiceProfile:
    """The cutoffs for ``model`` (default: the model the app ships). ``overrides`` that are not None
    replace single values, which is how a number saved in Settings or given in the lab wins."""
    base = PROFILES[model or models.EMBEDDING_NAME]
    given = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **given) if given else base


class Embedder(Protocol):
    def embed(self, audio: np.ndarray) -> np.ndarray | None: ...


class SherpaEmbedder:
    def __init__(self, models_root: Path | None = None, num_threads: int = 1, model_path: Path | None = None):
        """``model_path`` (the lab) runs another fingerprint model file than the installed one."""
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path or models.embedding_model(models_root)), num_threads=num_threads)
        if not cfg.validate():
            raise FileNotFoundError("Speaker embedding model is not installed")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        if len(audio) < MIN_FINGERPRINT_S * SAMPLE_RATE:
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

    def __init__(self, prefix: str, threshold: float = 0.75, min_new_speaker_s: float = 2.0,
                 weak_speaker_s: float = 2.0, adopt_threshold: float = 0.6):
        self.prefix = prefix
        self.threshold = threshold
        self.min_new_speaker_s = min_new_speaker_s
        # A speaker known only from short clips (under weak_speaker_s of speech) has an
        # unreliable fingerprint. Test #3 opened with a 1.0 s line that founded SPEAKER_01, so
        # the same voice's first long line (0.70 alike) became a new SPEAKER_03.
        self.weak_speaker_s = weak_speaker_s
        self.adopt_threshold = adopt_threshold
        self._centroids: list[np.ndarray | None] = []  # None: speaker seen without a fingerprint yet
        self._weights: list[float] = []  # seconds of speech behind each fingerprint
        self._last: int | None = None

    def assign(self, embedding: np.ndarray | None, duration_s: float) -> str:
        weight = max(duration_s, 0.1)
        if embedding is None or not len(embedding):
            if self._last is None:
                self._new(None, 0.0)
            return label(self.prefix, self._last + 1)

        scored = [(i, float(np.dot(embedding, c / (np.linalg.norm(c) or 1.0))))
                  for i, c in enumerate(self._centroids) if c is not None]
        if not scored:
            if self._centroids:  # fill the placeholder speaker
                self._update(0, embedding, weight)
                return label(self.prefix, 1)
            return label(self.prefix, self._new(embedding * weight, weight) + 1)

        idx, sim = max(scored, key=lambda item: item[1])
        if sim >= self.threshold:
            # Confident match: refine that speaker's fingerprint.
            self._update(idx, embedding, weight)
            return label(self.prefix, idx + 1)
        if duration_s >= self.min_new_speaker_s:
            # Before founding a new speaker, let a weakly-known speaker that is reasonably
            # close adopt this line; its fingerprint was built from too little audio to trust.
            weak = [(i, s) for i, s in scored
                    if self._weights[i] < self.weak_speaker_s and s >= self.adopt_threshold]
            if weak:
                widx, _ = max(weak, key=lambda item: item[1])
                self._update(widx, embedding, weight)
                return label(self.prefix, widx + 1)
            return label(self.prefix, self._new(embedding * weight, weight) + 1)
        # Too short to trust as a new voice: attach to the closest match without letting
        # it blur that speaker's fingerprint.
        self._last = idx
        return label(self.prefix, idx + 1)

    def _update(self, idx: int, embedding: np.ndarray, weight: float) -> None:
        current = self._centroids[idx]
        self._centroids[idx] = embedding * weight if current is None else current + embedding * weight
        self._weights[idx] += weight
        self._last = idx

    def _new(self, centroid: np.ndarray | None, weight: float) -> int:
        self._centroids.append(centroid)
        self._weights.append(weight)
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


def cluster_voices(vecs: list[np.ndarray], weights: list[float], threshold: float = 0.70,
                   names: list[str | None] | None = None) -> list[int]:
    """Average-linkage clustering of voice fingerprints over a whole meeting.

    Unlike the live pass, every line is compared with every other line, so the order lines
    arrive in doesn't matter. Cluster similarity is the weight-averaged cosine similarity of
    all cross pairs (weights are line durations, so long lines count more than "Yeah.").
    Merging stops when no two clusters are at least ``threshold`` alike.

    Threshold 0.70 (with the gated ``fold_small_clusters``) was chosen on kept recordings with
    ``tools/final_lab.py matrix``: every setting from 0.685 to 0.71 keeps Test #3's three speakers
    apart (confirmed different), keeps Test #2's two main speakers, keeps Test #4's 28 s speaker
    out of the large cluster and its 74 s speaker exactly as before, and gets the synthetic
    two-voice meeting right. Below 0.685 Test #4's 28 s speaker merges into the 325 s one; from
    0.715 the 74 s speaker takes in other lines. 0.65, the earlier default, merged that 28 s speaker.

    ``names[i]`` is the user's name for line ``i`` or None (L14). Two clusters that hold different names
    are never merged: their similarity is set to -inf at the start and again after every merge. Lines with
    the same name are merged into one cluster before the loop starts (L39), by the same weighted-average
    update the loop uses, so they always end in one cluster, however unlike their voices are. Nothing
    else about the linkage changes, so with no names (or all None) the result is exactly the same.
    """
    n = len(vecs)
    if n == 0:
        return []
    X = np.stack(vecs).astype(np.float64)
    w = np.asarray(weights, dtype=np.float64)
    S = X @ X.T  # line-to-line similarity
    np.fill_diagonal(S, -np.inf)
    W = w.copy()  # total weight per cluster
    active = np.ones(n, dtype=bool)
    members: list[list[int]] = [[i] for i in range(n)]
    cluster_name: list[str | None] = list(names) if names is not None and any(names) else [None] * n
    named = any(cluster_name)
    if named:
        # Same name, same cluster (L39): merge each name's pieces first, exactly as the loop below merges
        # two clusters (Lance-Williams), so similarity rows, weights and members are what they would be had
        # the loop done it. Done before the cannot-link marking, so no -inf is mixed into those rows.
        first: dict[str, int] = {}
        for j in range(n):
            nm = cluster_name[j]
            if nm is None:
                continue
            i = first.setdefault(nm, j)
            if i == j:
                continue
            new = (W[i] * S[i] + W[j] * S[j]) / (W[i] + W[j])
            S[i, :] = new
            S[:, i] = new
            S[i, i] = -np.inf
            S[j, :] = -np.inf
            S[:, j] = -np.inf
            W[i] += W[j]
            members[i] += members[j]
            members[j] = []
            active[j] = False
        for i in range(n):  # cannot-link: different names never share a cluster
            if not active[i]:
                continue
            if cluster_name[i] is not None:
                for j in range(n):
                    if cluster_name[j] is not None and cluster_name[j] != cluster_name[i]:
                        S[i, j] = -np.inf
    while active.sum() > 1:
        flat = int(np.argmax(S))
        i, j = divmod(flat, n)
        if S[i, j] < threshold:
            break
        # Lance-Williams update for weighted average linkage: merge j into i.
        new = (W[i] * S[i] + W[j] * S[j]) / (W[i] + W[j])
        S[i, :] = new
        S[:, i] = new
        S[i, i] = -np.inf
        if named:
            cluster_name[i] = cluster_name[i] or cluster_name[j]
            if cluster_name[i] is not None:
                for k in range(n):
                    if k != i and active[k] and cluster_name[k] not in (None, cluster_name[i]):
                        S[i, k] = S[k, i] = -np.inf
        S[j, :] = -np.inf
        S[:, j] = -np.inf
        W[i] += W[j]
        members[i] += members[j]
        members[j] = []
        active[j] = False
    labels = [0] * n
    clusters = sorted((m for m in members if m), key=min)
    for k, m in enumerate(clusters):
        for idx in m:
            labels[idx] = k
    return labels


def _centroid(vecs: list[np.ndarray], weights: list[float], members: list[int]) -> np.ndarray:
    c = np.sum([vecs[i] * weights[i] for i in members], axis=0)
    return c / (np.linalg.norm(c) or 1.0)


def fold_small_clusters(labels: list[int], vecs: list[np.ndarray], weights: list[float], *,
                        min_s: float = 4.0, min_fraction: float = 0.03, always_fold_s: float = 4.0,
                        min_similarity: float = 0.70, protected: set[int] | None = None,
                        cluster_names: dict[int, str] | None = None) -> list[int]:
    """Give clusters with little speech to their most similar larger cluster, if they sound alike.

    A cluster of one or two short lines ("I don't know.", 1.3 s) is usually a known voice
    whose short-clip fingerprint came out unusual, not a new person, so a cluster under
    ``always_fold_s`` is always folded. A bigger candidate (under the size limit, which is
    ``min_s`` or ``min_fraction`` of all speech) is a real voice more often than not: Test #4's
    28 s speaker was 0.64-0.75 like its target and was folded away. It is folded only when its
    centroid is at least ``min_similarity`` like the target's. Clusters in ``protected`` (they hold a
    name the user gave) are never folded into an unnamed or differently named cluster, which would hand
    the name to the wrong person. With ``cluster_names`` (cluster id -> its name) a protected cluster may
    still fold into a larger cluster that carries the same name, by the same rules. Since ``cluster_voices``
    puts every line of one name into one cluster, that case cannot arise from its output; it is kept as a
    guard for labels that did not come from it. Labels keep the
    target's cluster id; callers renumber.
    """
    protected = protected or set()
    totals: dict[int, float] = {}
    for lab, wt in zip(labels, weights):
        totals[lab] = totals.get(lab, 0.0) + wt
    speech = sum(totals.values())
    limit = max(min_s, min_fraction * speech)
    large = [k for k, v in totals.items() if v >= limit]
    if not large or len(large) == len(totals):
        return list(labels)
    cents = {k: _centroid(vecs, weights, [i for i in range(len(labels)) if labels[i] == k]) for k in large}
    out = list(labels)
    for k in totals:
        if k in large:
            continue
        pool = large
        if k in protected:
            pool = [j for j in large if cluster_names and cluster_names.get(j) is not None
                    and cluster_names.get(j) == cluster_names.get(k)]
            if not pool:
                continue
        idx = [i for i in range(len(labels)) if labels[i] == k]
        c = _centroid(vecs, weights, idx)
        target = max(pool, key=lambda j: float(np.dot(c, cents[j])))
        if totals[k] >= always_fold_s and float(np.dot(c, cents[target])) < min_similarity:
            continue
        for i in idx:
            out[i] = target
    return out


def merge_small_speakers(turns: list[Turn], centroids: dict[int, np.ndarray], min_s: float = 15.0,
                         min_fraction: float = 0.05, min_similarity: float = 0.75) -> list[Turn]:
    """Fold speakers with little speech into the most similar larger speaker.

    Offline clustering tends to spin off small clusters from a main voice (Test #2: three
    "speakers" with 2-10 s each, at 0.83-0.90 similarity to a main speaker). A small
    cluster is merged only if it really sounds like a larger one, so a genuinely
    different person who speaks briefly keeps their own label. (Test #3 had a wrong
    merge at 0.71, hence 0.75.) These turns now only decide where a line may be split;
    the final labels come from ``cluster_voices``.
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
    def __init__(self, models_root: Path | None = None, threshold: float | None = None, num_threads: int = 2,
                 merge_small: bool = True, embedding_path: Path | None = None):
        """``threshold`` None means the shipped model's ``split_cluster``. ``embedding_path`` (the lab)
        runs another fingerprint model file than the installed one."""
        import sherpa_onnx

        if threshold is None:
            threshold = profile().split_cluster
        self._embedding_path = embedding_path
        self._models_root = models_root
        self._merge_small = merge_small
        self._num_threads = num_threads
        self.last_timings: dict[str, float] = {}  # seconds of the last diarize(): "merge_small"

        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(models.segmentation_model(models_root))),
                num_threads=num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(embedding_path or models.embedding_model(models_root)), num_threads=num_threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not cfg.validate():
            raise FileNotFoundError("Diarization models are not installed")
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def diarize(self, audio: np.ndarray, progress: Callable[[float], None] | None = None) -> list[Turn]:
        self.last_timings = {}
        if len(audio) < SAMPLE_RATE:
            return []

        def cb(done: int, total: int) -> int:
            if progress and total:
                progress(done / total)
            return 0

        result = self._sd.process(audio.astype(np.float32), callback=cb).sort_by_start_time()
        turns = [Turn(int(r.start * 1000), int(r.end * 1000), int(r.speaker)) for r in result]
        if self._merge_small and len({t.speaker for t in turns}) > 1:
            t0 = time.perf_counter()
            centroids = speaker_centroids(audio, turns, SherpaEmbedder(self._models_root, self._num_threads,
                                                                       self._embedding_path))
            turns = merge_small_speakers(turns, centroids)
            self.last_timings["merge_small"] = time.perf_counter() - t0
        return turns
