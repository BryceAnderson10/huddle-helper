"""Recording session: capture -> VAD -> transcription -> merge -> JSONL.

Threads:
- capture callbacks push aligned 16 kHz audio into one inbox per stream;
- one worker per stream writes the FLAC buffer and runs VAD;
- one transcription worker takes utterances oldest-first across both streams
  (so a lagging transcriber works through the backlog without dropping speech),
  adds live speaker labels and hands segments to the merger;
- a ticker publishes per-stream watermarks so the merger can release lines.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import meeting as mt, vocab
from .audio import SAMPLE_RATE
from .audio.buffer import FlacBuffer
from .config import Settings
from .diarize import MIC_PREFIX, REMOTE, SYSTEM_PREFIX, YOU, Embedder, OnlineClusterer
from .finalize import append_words
from .segments import Segment, Word
from .transcribe import Transcriber
from .vad import FrameScorer, Utterance, UtteranceSegmenter
from .writer import JsonlWriter, SegmentMerger, meeting_end_event, meeting_start_event

log = logging.getLogger(__name__)

SOURCES = ("mic", "system")


@dataclass
class Components:
    """Pluggable pieces, so tests can swap in fakes."""
    transcriber: Transcriber
    embedder: Embedder | None = None
    scorer_factory: Callable[[], FrameScorer] | None = None
    capture_factory: Callable[..., object] | None = None
    model_info: dict = field(default_factory=dict)


@dataclass
class Status:
    state: str = "idle"  # idle | recording | stopping | finalizing | error
    meeting_id: str | None = None
    elapsed_s: float = 0.0
    lag_s: float = 0.0
    backlog: int = 0
    segments: int = 0
    detail: str = ""


class _StreamWorker:
    def __init__(self, source: str, rec: "Recorder", scorer: FrameScorer, buffer: FlacBuffer,
                 settings: Settings):
        self.source = source
        self.rec = rec
        self.inbox: queue.Queue[np.ndarray | None] = queue.Queue()
        self.buffer = buffer
        self.lock = threading.RLock()  # re-entered when the segmenter emits an utterance
        self.outstanding: dict[int, tuple[int, int]] = {}  # utterance id -> (start, end) samples
        self.done = False
        self.segmenter = UtteranceSegmenter(
            source, self._on_utterance, scorer=scorer, threshold=settings.vad_threshold,
            min_silence_ms=settings.min_silence_ms, max_utterance_s=settings.max_utterance_s,
        )
        self.thread = threading.Thread(target=self._run, name=f"stream-{source}", daemon=True)

    def _on_utterance(self, utt: Utterance) -> None:
        uid = next(self.rec._ids)
        with self.lock:
            self.outstanding[uid] = (utt.start, utt.end)
        self.rec._utterances.put((utt.start, uid, utt))

    def _run(self) -> None:
        while True:
            chunk = self.inbox.get()
            if chunk is None:
                break
            try:
                self.buffer.write(chunk)
                with self.lock:
                    self.segmenter.feed(chunk)
            except Exception:  # noqa: BLE001
                log.exception("Error processing %s audio", self.source)
        with self.lock:
            self.segmenter.flush()
            self.done = True
        self.buffer.close()

    def watermark(self) -> int:
        """Samples: no segment from this stream will start before this point."""
        with self.lock:
            wm = self.segmenter.watermark if not self.done else 1 << 62
            if self.outstanding:
                wm = min(wm, min(s for s, _ in self.outstanding.values()))
            return wm

    def oldest_pending_end(self) -> int | None:
        """End sample of the oldest utterance still waiting for transcription."""
        with self.lock:
            return min((e for _, e in self.outstanding.values()), default=None)

    def finish(self, uid: int) -> None:
        with self.lock:
            self.outstanding.pop(uid, None)


class Recorder:
    def __init__(self, settings: Settings, components: Components,
                 on_status: Callable[[Status], None] | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.settings = settings
        self.c = components
        self._on_status = on_status or (lambda s: None)
        self._on_error = on_error or (lambda msg: None)
        self.status = Status()
        self.folder: mt.MeetingFolder | None = None
        self._ids = itertools.count(1)
        self._utterances: queue.PriorityQueue = queue.PriorityQueue()
        self._workers: dict[str, _StreamWorker] = {}
        self._clusterers: dict[str, OnlineClusterer] = {}
        self._writer: JsonlWriter | None = None
        self._merger: SegmentMerger | None = None
        self._capture = None
        self._t0 = 0.0
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._stop_tick = threading.Event()
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self.latencies_ms: list[int] = []  # utterance end -> line written (wall clock)
        self._utt_end_wall: dict[tuple[str, int], float] = {}
        self._speech_peaks: dict[str, list[float]] = {src: [] for src in SOURCES}
        self._quiet_mic_warned = False
        self._max_backlog = 0
        self._max_lag_s = 0.0
        self._vocab_lock = threading.Lock()
        self._meeting_terms: list[str] = []
        self._settings_terms: list[str] = []
        self._terms_used: list[str] = []
        self._prompt: str | None = None
        self._vocab_changed_at_ms: list[int] = []

    # -- public API -----------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.status.state == "recording"

    @property
    def meeting_terms(self) -> list[str]:
        return list(self._meeting_terms)

    @property
    def vocabulary_used(self) -> list[str]:
        return list(self._terms_used)

    def _apply_vocabulary(self) -> bool:
        """Recompute the prompt from the meeting and Settings terms. True if the terms used changed."""
        used, _dropped = vocab.fit_budget(self._meeting_terms, self._settings_terms,
                                          vocab.counter_for(self.c.transcriber) or vocab.estimate_tokens)
        changed = used != self._terms_used
        self._terms_used = used
        self._prompt = vocab.build_prompt(used)
        return changed

    def set_vocabulary(self, settings_terms: list[str] | None = None,
                       meeting_terms: list[str] | None = None) -> None:
        """Change the vocabulary during a meeting. Applies from the next utterance; lines already
        written stay as they are. A change to the terms actually used is noted in meta.json."""
        with self._vocab_lock:
            if settings_terms is not None:
                self._settings_terms = list(settings_terms)
            if meeting_terms is not None:
                self._meeting_terms = list(meeting_terms)
            changed = self._apply_vocabulary()
            if not changed or self.folder is None or self.status.state not in ("recording", "stopping"):
                return
            self._vocab_changed_at_ms.append(int((time.perf_counter() - self._t0) * 1000))
            self.folder.update_meta(vocabulary=list(self._terms_used),
                                    vocabulary_changed_at_ms=list(self._vocab_changed_at_ms))

    def start(self, title: str, meeting_terms: Iterable[str] = ()) -> mt.MeetingFolder:
        if self.status.state not in ("idle", "error"):
            raise RuntimeError("Already recording")
        s = self.settings
        with self._vocab_lock:
            self._meeting_terms = list(meeting_terms)
            self._settings_terms = list(s.vocabulary)
            self._terms_used = []
            self._vocab_changed_at_ms = []
            self._apply_vocabulary()
        started = mt.now_local()
        meta = {
            "devices": {"mic": s.mic_device or "default", "system": s.output_device or "default"},
            "models": {
                "asr": getattr(self.c.transcriber, "name", "unknown"),
                "asr_compute_type": "int8",
                "vad": "silero_vad_v6",
                **self.c.model_info,
            },
            "settings": {
                "diarize_system": s.diarize_system, "diarize_mic": s.diarize_mic,
                "echo_dedupe": s.echo_dedupe, "keep_audio": s.keep_audio,
            },
            "vocabulary": list(self._terms_used),
            "vocabulary_changed_at_ms": [],
        }
        self.folder = mt.MeetingFolder.create(s.resolved_meetings_dir(), title, started, meta)
        self._writer = JsonlWriter(self.folder.live_path)
        self._writer.write(meeting_start_event(self.folder.meeting_id, mt.iso(started)))

        self._merger = SegmentMerger(SOURCES, self._write_segment, echo_dedupe=s.echo_dedupe)
        scorer_factory = self.c.scorer_factory
        if scorer_factory is None:
            from .vad import SileroScorer
            scorer_factory = SileroScorer
        for src in SOURCES:
            self._workers[src] = _StreamWorker(src, self, scorer_factory(),
                                               FlacBuffer(self.folder.audio_file(src)), s)
        if s.diarize_system:
            self._clusterers["system"] = OnlineClusterer(SYSTEM_PREFIX, s.live_speaker_threshold,
                                                         s.live_min_new_speaker_s)
        if s.diarize_mic:
            self._clusterers["mic"] = OnlineClusterer(MIC_PREFIX, s.live_speaker_threshold,
                                                      s.live_min_new_speaker_s)

        capture_factory = self.c.capture_factory
        if capture_factory is None:
            from .audio.capture import WasapiCapture
            capture_factory = WasapiCapture
        self._capture = capture_factory(
            on_audio=self._on_audio, mic_device=s.mic_device, output_device=s.output_device,
            on_gap=self._on_gap, on_error=self._on_error,
        )

        self._stop_tick.clear()
        self._seq = 0
        self.latencies_ms = []
        self._utt_end_wall = {}
        self._speech_peaks = {src: [] for src in SOURCES}
        self._quiet_mic_warned = False
        self._max_backlog = 0
        self._max_lag_s = 0.0
        self.status = Status(state="recording", meeting_id=self.folder.meeting_id)
        for w in self._workers.values():
            w.thread.start()
        self._threads = [
            threading.Thread(target=self._transcribe_loop, name="transcribe", daemon=True),
            threading.Thread(target=self._tick_loop, name="watermarks", daemon=True),
        ]
        for t in self._threads:
            t.start()
        self._t0 = time.perf_counter()
        self._capture.start(self._t0)
        names = getattr(self._capture, "device_names", {})
        if any(names.values()):
            self.folder.update_meta(devices={k: v for k, v in names.items()})
        log.info("Recording started: %s", self.folder.path)
        self._publish()
        return self.folder

    def stop(self) -> mt.MeetingFolder:
        """Stop capture and drain the backlog; the live file is complete on return."""
        if self.status.state != "recording":
            raise RuntimeError("Not recording")
        self.status.state = "stopping"
        self._publish()
        self._capture.stop()
        ended = mt.now_local()
        for w in self._workers.values():
            w.inbox.put(None)
        for w in self._workers.values():
            w.thread.join()
        self._utterances.join()  # transcribe everything still queued
        self._utterances.put((1 << 62, 0, None))
        self._stop_tick.set()
        for t in self._threads:
            t.join()
        self._merger.flush_all()
        count = self._seq
        self._writer.write(meeting_end_event(mt.iso(ended), count))
        self._writer.close()
        self.folder.update_meta(
            status=mt.STATUS_PROCESSING, ended_at=mt.iso(ended), clean_exit=True, segments=count,
            echo_segments_dropped=self._merger.dropped_echo,
            duration_s=round(time.perf_counter() - self._t0, 1),
            live_stats=self.live_stats(),
        )
        folder = self.folder
        self._workers.clear()
        self._clusterers.clear()
        self.status = Status(state="idle", meeting_id=folder.meeting_id, segments=count)
        self._publish()
        log.info("Recording stopped: %s (%d segments)", folder.path, count)
        return folder

    def live_stats(self) -> dict:
        """Delay and level figures saved to meta.json, so every recording measures itself."""
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> int | None:
            return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

        return {
            # Wall-clock time from the end of an utterance to its line being on disk (PRD: p90 <= 5 s).
            "line_delay_ms": {"median": pct(0.5), "p90": pct(0.9), "max": lat[-1] if lat else None,
                              "lines": len(lat)},
            "max_backlog": self._max_backlog,
            "max_lag_s": round(self._max_lag_s, 1),
            # Median peak level of detected speech, 0-1. Below ~0.05 the mic is too quiet.
            "speech_peak_median": {src: round(float(np.median(p)), 3) if p else None
                                   for src, p in self._speech_peaks.items()},
        }

    # -- callbacks ------------------------------------------------------
    def _on_audio(self, source: str, samples: np.ndarray) -> None:
        w = self._workers.get(source)
        if w is not None:
            w.inbox.put(samples)

    def _on_gap(self, source: str, start_ms: int, end_ms: int, reason: str) -> None:
        if self.folder is not None and end_ms > start_ms:
            self.folder.add_gap(source, start_ms, end_ms, reason)

    def _write_segment(self, seg: Segment) -> None:
        with self._seq_lock:
            self._seq += 1
            self._writer.write(seg.to_event(self._seq))
            wall = self._utt_end_wall.pop((seg.source, seg.start_ms), None)
            if wall is not None and getattr(self._capture, "realtime", True):
                self.latencies_ms.append(int((time.perf_counter() - wall) * 1000))

    # -- workers --------------------------------------------------------
    def _speaker(self, utt: Utterance, words: list[Word] | None = None) -> str:
        """Provisional speaker label. ``words`` (with timings) give the speech span for the clusterer.

        Test #3: a 1.6 s line measured as 2.1 s with its VAD padding and trailing silence, which
        was enough to found a speaker. Without word timings the utterance's duration is used.
        """
        clusterer = self._clusterers.get(utt.source)
        if clusterer is None:
            return YOU if utt.source == "mic" else REMOTE
        emb = None
        if self.c.embedder is not None:
            try:
                emb = self.c.embedder.embed(utt.audio)
            except Exception:  # noqa: BLE001
                log.exception("Speaker embedding failed")
        speech_s = (words[-1].end_ms - words[0].start_ms) / 1000 if words else utt.duration_s
        return clusterer.assign(emb, speech_s)

    def _transcribe_loop(self) -> None:
        while True:
            _start, uid, utt = self._utterances.get()
            if utt is None:
                self._utterances.task_done()
                break
            self._in_flight = 1
            worker = self._workers[utt.source]
            try:
                result = self.c.transcriber.transcribe(utt.audio, prompt=self._prompt)
                if result.text:
                    offset = utt.start_ms
                    words = [Word(offset + w.start_ms, offset + w.end_ms, w.text) for w in result.words]
                    start_ms = max(words[0].start_ms, utt.start_ms) if words else utt.start_ms
                    end_ms = min(words[-1].end_ms, utt.end_ms) if words else utt.end_ms
                    if end_ms <= start_ms:
                        start_ms, end_ms = utt.start_ms, utt.end_ms
                    seg = Segment(utt.source, start_ms, end_ms, result.text, result.confidence,
                                  self._speaker(utt, words), words)
                    append_words(self.folder.words_path, seg)
                    self._utt_end_wall[(seg.source, seg.start_ms)] = self._wall_for(utt.end)
                    self._merger.add(seg)
                    self._track_level(utt)
            except Exception:  # noqa: BLE001 - one bad utterance must not stop the meeting
                log.exception("Transcription failed for %s utterance at %d ms", utt.source, utt.start_ms)
            finally:
                worker.finish(uid)
                self._in_flight = 0
                self._utterances.task_done()
                self._push_watermarks()

    QUIET_MIC_PEAK = 0.05

    def _track_level(self, utt: Utterance) -> None:
        peaks = self._speech_peaks[utt.source]
        peaks.append(float(np.abs(utt.audio).max()) if len(utt.audio) else 0.0)
        if (utt.source == "mic" and not self._quiet_mic_warned and len(peaks) >= 2
                and float(np.median(peaks)) < self.QUIET_MIC_PEAK):
            self._quiet_mic_warned = True
            log.warning("Microphone speech peaks at %.3f; input level is very low", float(np.median(peaks)))
            self._on_error("Your microphone is very quiet. Raise its input level in Windows Sound "
                           "settings for more accurate transcripts.")

    def _wall_for(self, sample: int) -> float:
        return self._t0 + sample / SAMPLE_RATE

    def _push_watermarks(self) -> None:
        if self._merger is None:
            return
        for src, w in self._workers.items():
            self._merger.update_watermark(src, w.watermark() * 1000 // SAMPLE_RATE)

    def _tick_loop(self) -> None:
        while not self._stop_tick.wait(0.2):
            try:
                self._push_watermarks()
                self._publish()
            except Exception:  # noqa: BLE001
                log.exception("Watermark tick failed")

    def _publish(self) -> None:
        st = self.status
        if st.state in ("recording", "stopping") and self._t0:
            st.elapsed_s = time.perf_counter() - self._t0
            # Lag: how long ago the oldest not-yet-transcribed utterance ended.
            ends = [e for w in self._workers.values() if (e := w.oldest_pending_end()) is not None]
            st.lag_s = max(0.0, st.elapsed_s - min(ends) / SAMPLE_RATE) if ends else 0.0
            st.backlog = self._utterances.qsize() + self._in_flight
            st.segments = self._seq
            if st.state == "recording":
                self._max_backlog = max(self._max_backlog, st.backlog)
                self._max_lag_s = max(self._max_lag_s, st.lag_s)
        try:
            self._on_status(st)
        except Exception:  # noqa: BLE001
            log.exception("Status callback failed")


def load_components(settings: Settings, models_root: Path | None = None) -> Components:
    """Build the real model-backed components (requires installed models)."""
    from . import models
    from .transcribe import WhisperTranscriber

    transcriber = WhisperTranscriber(settings.whisper_model, settings.beam_size, settings.cpu_threads,
                                     models_root)
    embedder = None
    info = {}
    if settings.needs_diarization:
        from .diarize import SherpaEmbedder

        embedder = SherpaEmbedder(models_root)
        info = {"diarization_segmentation": models.SEGMENTATION_NAME,
                "diarization_embedding": models.EMBEDDING_NAME}
    return Components(transcriber=transcriber, embedder=embedder, model_info=info)
