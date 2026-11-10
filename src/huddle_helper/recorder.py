"""Recording session: capture -> VAD -> transcription -> merge -> JSONL.

Threads:
- capture callbacks push aligned 16 kHz audio into one inbox per stream;
- one worker per stream writes the FLAC buffer and runs VAD;
- one transcription worker takes utterances oldest-first across both streams
  (so a lagging transcriber works through the backlog without dropping speech),
  adds live speaker labels and hands segments to the merger;
- a ticker publishes per-stream watermarks so the merger can release lines.

The mic passes through ``MicGate`` first (``_on_audio``): while the user is muted its samples are
replaced by zeros before anything else sees them. Bookmarks go through ``MarkLog``.

Click to hear (WP7): each stream worker also writes the (gated) audio to a ``RecentAudio`` ring, the last
10 minutes in memory, which clips are played from while recording. A clip played while recording is
logged as a playback window (``note_playback``); the transcribe loop drops an utterance that overlaps
a window and repeats its words, before a speaker is assigned, and counts it for meta.json.
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

from . import attendees as attendees_mod, marks as marks_mod, meeting as mt, playback, vocab
from .audio import SAMPLE_RATE
from .audio.buffer import FlacBuffer
from .audio.gate import MicGate
from .audio.ring import RecentAudio
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
    # Decaying peak level (0-1) of each stream for the window's meters; updated every 200 ms.
    levels: dict[str, float] = field(default_factory=lambda: {"mic": 0.0, "system": 0.0})
    mic_muted: bool = False  # the user has taken their mic out of the recording (MicGate)
    marks: int = 0  # bookmarks in this meeting


class _StreamWorker:
    def __init__(self, source: str, rec: "Recorder", scorer: FrameScorer, buffer: FlacBuffer,
                 settings: Settings, ring: RecentAudio | None = None):
        self.source = source
        self.rec = rec
        self.inbox: queue.Queue[np.ndarray | None] = queue.Queue()
        self.buffer = buffer
        self.ring = ring
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
                if self.ring is not None:
                    self.ring.write(chunk)
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
                 on_error: Callable[[str], None] | None = None,
                 before_reopen: Callable[[], None] | None = None):
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
        self._levels: dict[str, tuple[float, float]] = {}  # source -> (peak, perf_counter time)
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
        self.gate: MicGate | None = None
        self.marklog: marks_mod.MarkLog | None = None
        self._mute_lock = threading.Lock()
        self._pending_gaps: list[tuple[int, int]] = []  # closed muted periods not yet in meta.json
        self._written_since: int | None = None
        # Click to hear: called by the capture before it re-opens its devices (the controller stops playback).
        self.before_reopen = before_reopen
        self.playbacks = playback.PlaybackWindows()
        self._playback_written: tuple[int, int] | None = None

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

    def start(self, title: str, meeting_terms: Iterable[str] = (), start_muted: bool = False,
              attendees: Iterable[str] = ()) -> mt.MeetingFolder:
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
            "mic_muted_since_ms": 0 if start_muted else None,
        }
        names = attendees_mod.clean_attendees(attendees)
        if names:  # additive (L13): absent when none were entered
            meta["attendees"] = names
        self.folder = mt.MeetingFolder.create(s.resolved_meetings_dir(), title, started, meta)
        self._writer = JsonlWriter(self.folder.live_path)
        self.gate = MicGate(muted=start_muted)
        self.marklog = marks_mod.MarkLog(self.folder)
        self._pending_gaps, self._written_since = [], 0 if start_muted else None
        self._writer.write(meeting_start_event(self.folder.meeting_id, mt.iso(started)))

        self._merger = SegmentMerger(SOURCES, self._write_segment, echo_dedupe=s.echo_dedupe)
        scorer_factory = self.c.scorer_factory
        if scorer_factory is None:
            from .vad import SileroScorer
            scorer_factory = SileroScorer
        for src in SOURCES:
            self._workers[src] = _StreamWorker(src, self, scorer_factory(),
                                               FlacBuffer(self.folder.audio_file(src)), s,
                                               RecentAudio(playback.RING_SECONDS))
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
            on_gap=self._on_gap, on_error=self._on_error, before_reopen=self._before_reopen,
        )
        self.playbacks = playback.PlaybackWindows()
        self._playback_written = None

        self._stop_tick.clear()
        self._seq = 0
        self.latencies_ms = []
        self._utt_end_wall = {}
        self._speech_peaks = {src: [] for src in SOURCES}
        self._quiet_mic_warned = False
        self._levels = {}
        self._max_backlog = 0
        self._max_lag_s = 0.0
        self.status = Status(state="recording", meeting_id=self.folder.meeting_id, mic_muted=start_muted)
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
        self.gate.finish()  # Stop always unmutes; an open muted period ends with the audio
        self.status.mic_muted = False
        self._sync_mute()
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
            playbacks=self.playbacks.count, playback_segments_dropped=self.playbacks.dropped,
            duration_s=round(time.perf_counter() - self._t0, 1),
            live_stats=self.live_stats(),
            marks=self.marklog.count, mark_kinds=marks_mod.custom_kinds(self.marklog.state().values()),
        )
        folder = self.folder
        self._workers.clear()
        self._clusterers.clear()
        self.status = Status(state="idle", meeting_id=folder.meeting_id, segments=count)
        self._publish()
        log.info("Recording stopped: %s (%d segments)", folder.path, count)
        return folder

    # -- click to hear (WP7) ---------------------------------------------------------------
    def _before_reopen(self) -> None:
        cb = self.before_reopen
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 - the capture must go on re-binding
                log.exception("before_reopen failed")

    @property
    def echo_dropped(self) -> int:
        """Mic lines the echo dedupe has dropped so far (a sign that there are no headphones)."""
        return self._merger.dropped_echo if self._merger is not None else 0

    def recent_oldest_ms(self) -> dict[str, int]:
        """The earliest meeting time the in-memory audio of each stream still holds."""
        return {src: w.ring.oldest_ms for src, w in self._workers.items() if w.ring is not None}

    def recent_clip(self, clip: playback.Clip):
        """The audio of ``clip`` from the rings (what the capture delivered after the mic gate), levelled
        for playback; None when it has left the 10-minute window."""
        rings = {src: w.ring for src, w in self._workers.items() if w.ring is not None}
        if len(clip.sources) == 1:
            return playback.clip_from_ring(rings.get(clip.sources[0]), clip.start_ms, clip.end_ms, clip.pad_ms)
        return playback.mixed_from_rings(rings, clip)

    def note_playback(self, duration_s: float, text: str, run: bool = False) -> bool:
        """A clip of ``duration_s`` is about to play: open a playback window for it (the drop rule).
        ``run``: one clip of a continuous Play from here, which also gets a joined window with the
        clips just before it. False when not recording (nothing to drop)."""
        now = self.now_ms()
        if now is None or self.status.state != "recording":
            return False
        if run:
            self.playbacks.add_run_clip(now, duration_s, text)
        else:
            self.playbacks.add(now, duration_s, text)
        return True

    def end_playback_run(self) -> None:
        self.playbacks.end_run()

    def _sync_playbacks(self) -> None:
        """Write the playback counts to meta.json when they changed (on the ticker, never the capture path)."""
        counts = self.playbacks.snapshot()
        if self.folder is None or counts == self._playback_written:
            return
        try:
            self.folder.update_meta(playbacks=counts[0], playback_segments_dropped=counts[1])
            self._playback_written = counts
        except Exception:  # noqa: BLE001 - tried again at the next tick
            log.exception("Could not record the playback counts in meta.json")

    # -- mute and bookmarks -------------------------------------------------------------
    def now_ms(self) -> int | None:
        """The meeting clock (ms since the recording started), None when not recording."""
        if self.status.state not in ("recording", "stopping") or not self._t0:
            return None
        return int((time.perf_counter() - self._t0) * 1000)

    @property
    def mic_muted(self) -> bool:
        return self.gate is not None and self.status.state == "recording" and self.gate.muted

    def set_mic_muted(self, muted: bool) -> bool:
        """Take the mic out of the recording (or put it back). Returns the state afterwards."""
        if self.status.state != "recording" or self.gate is None:
            return False
        if muted:
            self.gate.mute()
        else:
            self.gate.unmute()
        self.status.mic_muted = self.gate.muted
        self._sync_mute()
        self._publish()
        return self.gate.muted

    def _sync_mute(self) -> None:
        """Write closed muted periods and ``mic_muted_since_ms`` to meta.json (never on the capture path)."""
        if self.gate is None or self.folder is None:
            return
        with self._mute_lock:
            self._pending_gaps += self.gate.take_closed()
            since = self.gate.muted_since_ms
            if not self._pending_gaps and since == self._written_since:
                return
            try:
                self.folder.record_mute(self._pending_gaps, since)
            except Exception:  # noqa: BLE001 - tried again at the next tick
                log.exception("Could not record the muted period in meta.json")
                return
            self._pending_gaps, self._written_since = [], since

    def bookmark(self, kind: str = marks_mod.BOOKMARK, via: str = "hotkey", at_ms: int | None = None,
                 seq: int | None = None, at: str | None = None) -> marks_mod.MarkResult | None:
        """Add a bookmark at the meeting clock (see ``MarkLog`` for the 2 s rule). None when idle."""
        if self.marklog is None or self.status.state not in ("recording", "stopping"):
            return None
        when_ms = self.now_ms() if at_ms is None else at_ms
        result = self.marklog.add(kind, when_ms or 0, via, seq=seq, at=at)
        self.status.marks = self.marklog.count
        return result

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
    LEVEL_HALF_LIFE_S = 0.3  # how fast a meter falls after a peak

    def _on_audio(self, source: str, samples: np.ndarray) -> None:
        if source == "mic" and self.gate is not None:
            samples = self.gate.process(samples)  # zeros while muted: nothing downstream sees the real audio
        w = self._workers.get(source)
        if w is not None:
            w.inbox.put(samples)
        if len(samples):  # on the capture path: one abs().max() and a pow per chunk
            now = time.perf_counter()
            prev, then = self._levels.get(source, (0.0, now))
            held = prev * 0.5 ** ((now - then) / self.LEVEL_HALF_LIFE_S)
            self._levels[source] = (max(float(np.abs(samples).max()), held), now)

    def _current_levels(self) -> dict[str, float]:
        now = time.perf_counter()
        return {src: round(min(1.0, v * 0.5 ** ((now - t) / self.LEVEL_HALF_LIFE_S)), 3)
                for src, (v, t) in list(self._levels.items())} | {
                    src: 0.0 for src in SOURCES if src not in self._levels}

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
                    if self.playbacks.match(start_ms, end_ms, result.text):
                        # A clip the user played came back through the loopback or the mic: not a line, and
                        # its voice must not reach the speaker clustering (plan decision 2). The words' own
                        # span is compared with the playback window, not the utterance's: the VAD span runs
                        # on over the pause after the speech, which would catch the line the clip was taken
                        # from when it is played back a moment after it was said.
                        log.info("Dropped a played clip heard again at %d ms", start_ms)
                        continue
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
                self._sync_mute()
                self._sync_playbacks()
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
            st.levels = self._current_levels()
            if self.marklog is not None:
                st.marks = self.marklog.count
            if self.gate is not None and st.state == "recording":
                st.mic_muted = self.gate.muted
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
