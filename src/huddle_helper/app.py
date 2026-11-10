"""Application controller: owns settings, models, the recorder and background jobs.

UI-agnostic; the tray (tray.py) drives it and renders its state.
"""

from __future__ import annotations

import logging
import queue
import statistics
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, housekeeping, meeting as mt, models, vocab
from .recorder import Components, Recorder, Status, load_components

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinalizeProgress:
    """Where the after-Stop pass is, for the window's status bar (the tray reads ``finalizing``)."""
    meeting_id: str
    stage: str  # "Identifying speakers (system)", "Grouping voices", "Writing final transcript"
    frac: float = 0.0  # 0-1 within the stage
    overall: float = 0.0  # 0-1 over the whole pass, from fixed stage weights (see pass_fraction)
    elapsed_s: float = 0.0  # since this meeting's pass started
    eta_s: float | None = None  # None until there is history to estimate from: show the stage only


@dataclass
class AppState:
    phase: str = "starting"  # starting | downloading | ready | recording | stopping | error
    recording: Status | None = None
    finalizing: str = ""  # "<stage> 40%" (kept for the tray)
    finalize: FinalizeProgress | None = None  # structured form of the same, None when idle
    finalize_queue: int = 0
    message: str = ""
    last_meeting: str | None = None


# -- after-Stop time estimate (pure) --------------------------------------
SPEAKER_STAGE = "Identifying speakers"
VOICE_STAGE = "Grouping voices"
DIARIZE_SHARE = 0.87  # share of a pass spent in diarization, the rest in voice fingerprints
#                       (final_stats of the test meetings: diarize 87-91%, embed_pieces 9-13%)
OWN_RATE_FROM = 0.10  # blend in the pass's own rate once this fraction of the pass is done
HISTORY = 5  # completed meetings whose final_stats.speed are used


def pass_fraction(stage: str, frac: float, diarized: list[str] | tuple[str, ...]) -> float:
    """Fraction (0-1) of the whole pass done, from the stage name and the fraction within it.

    ``diarized`` are the streams the pass diarizes, in the order it does them (sorted by name).
    Diarization is DIARIZE_SHARE of the time, split equally over the streams; voice fingerprints
    are the rest. "Writing final transcript" counts as the end.
    """
    frac = min(1.0, max(0.0, frac))
    n = len(diarized)
    if stage.startswith(SPEAKER_STAGE):
        src = stage[stage.find("(") + 1:stage.find(")")] if "(" in stage else ""
        idx = list(diarized).index(src) if src in diarized else 0
        return DIARIZE_SHARE * (idx + frac) / max(n, 1)
    if stage == VOICE_STAGE:
        base = DIARIZE_SHARE if n else 0.0
        return base + (1.0 - base) * frac
    return 1.0 if stage.startswith("Writing") else frac


def estimate_remaining_s(overall: float, elapsed_s: float, length_s: float | None,
                         speeds: list[float]) -> float | None:
    """Seconds left in the after-Stop pass, or None when there is nothing to base it on.

    ``speeds`` are ``final_stats.speed`` (pass time / audio time) of the last completed meetings.
    expected total = median(speeds) x this meeting's length; once the pass is past OWN_RATE_FROM it
    is blended with the pass's own rate (elapsed / overall), weighted by how far along it is;
    remaining = (1 - overall) x expected total. With no history, or no length, there is no estimate
    (the window shows the stage and percent only).
    """
    if not speeds or not length_s or length_s <= 0:
        return None
    overall = min(1.0, max(0.0, overall))
    expected = statistics.median(speeds) * length_s
    if overall > OWN_RATE_FROM and elapsed_s > 0:
        expected = (1.0 - overall) * expected + overall * (elapsed_s / overall)
    return max(0.0, (1.0 - overall) * expected)


def meeting_length_s(meta: dict) -> float | None:
    """How long the meeting was, from meta.json (the recorder's duration_s, else start to end)."""
    if meta.get("duration_s"):
        return float(meta["duration_s"])
    try:
        start = datetime.fromisoformat(meta["started_at"])
        end = datetime.fromisoformat(meta["ended_at"])
        return max(0.0, (end - start).total_seconds()) or None
    except (KeyError, TypeError, ValueError):
        return None


def recent_final_speeds(meetings_dir: Path, exclude: str | None = None, limit: int = HISTORY) -> list[float]:
    """``final_stats.speed`` of the last ``limit`` completed meetings (by when their final file was written)."""
    found: list[tuple[str, float]] = []
    for f in mt.iter_meetings(meetings_dir):
        if f.meeting_id == exclude:
            continue
        try:
            meta = f.read_meta()
        except (OSError, ValueError):
            continue
        speed = (meta.get("final_stats") or {}).get("speed")
        if (meta.get("status") in (mt.STATUS_COMPLETE, mt.STATUS_INTERRUPTED)
                and isinstance(speed, (int, float)) and speed > 0):
            found.append((str(meta.get("final_written_at") or meta.get("ended_at") or f.meeting_id), float(speed)))
    found.sort()
    return [speed for _, speed in found[-limit:]]


def _same_apart_from_vocabulary(a: config.Settings, b: config.Settings) -> bool:
    return {**a.to_dict(), "vocabulary": []} == {**b.to_dict(), "vocabulary": []}


class AppController:
    def __init__(self, on_change: Callable[[], None] | None = None,
                 notify: Callable[[str], None] | None = None,
                 overrides: dict | None = None, capture_factory: Callable[..., object] | None = None):
        """``overrides`` (dev aids such as ``tray --out``) replace Settings fields for this run only:
        they are never written back to settings.json. ``capture_factory`` replaces the live devices
        (``tray --replay``)."""
        self.settings = config.load()
        self._overrides = dict(overrides or {})
        self._persisted = {k: getattr(self.settings, k) for k in self._overrides}
        for k, v in self._overrides.items():
            setattr(self.settings, k, v)
        self._capture_factory = capture_factory
        self.state = AppState()
        self._on_change = on_change or (lambda: None)
        self._notify = notify or (lambda msg: None)
        self._components: Components | None = None
        self._components_key = None
        self._recorder: Recorder | None = None
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._live_ids: set[str] = set()
        threading.Thread(target=self._finalize_worker, name="finalize", daemon=True).start()

    # -- lifecycle ------------------------------------------------------
    def boot(self) -> None:
        """Install models if needed, recover crashed meetings, purge old audio."""
        threading.Thread(target=self._boot, name="boot", daemon=True).start()

    def _boot(self) -> None:
        try:
            self._prepare_models()
            from .cli import go_offline
            go_offline()
            meetings_dir = self.settings.resolved_meetings_dir()
            for meeting_id in housekeeping.recover(meetings_dir, None, skip=self._live_ids):
                self._jobs.put(mt.MeetingFolder(meetings_dir / meeting_id))
                self.state.finalize_queue += 1
            self.purge()
            self._get_components()
            self._set(phase="ready", message="")
        except Exception as exc:  # noqa: BLE001
            log.exception("Startup failed")
            self._set(phase="error", message=f"Startup failed: {exc}")

    def _prepare_models(self) -> None:
        s = self.settings
        missing = models.missing_models(s.whisper_model, s.needs_diarization)
        if missing:
            self._set(phase="downloading", message=f"Downloading models: {', '.join(missing)}")
            self._notify("Downloading speech models (one-time). Recording is available when done.")
            models.install_all(s.whisper_model, s.needs_diarization,
                               progress=lambda m: self._set(message=m))
            self._notify("Models installed. Huddle Helper is ready.")

    def _get_components(self) -> Components:
        s = self.settings
        key = (s.whisper_model, s.beam_size, s.cpu_threads, s.needs_diarization)
        with self._lock:
            if self._components is None or self._components_key != key:
                log.info("Loading models for %s", key)
                self._components = load_components(s)
                self._components_key = key
            if self._capture_factory is not None:
                self._components.capture_factory = self._capture_factory
            return self._components

    def purge(self) -> None:
        try:
            housekeeping.purge_old_audio(self.settings.resolved_meetings_dir(), self.settings.auto_purge_days)
        except Exception:  # noqa: BLE001
            log.exception("Audio purge failed")

    # -- recording ------------------------------------------------------
    @property
    def can_start(self) -> bool:
        return self.state.phase == "ready"

    @property
    def is_recording(self) -> bool:
        return self.state.phase == "recording"

    def current_folder(self) -> mt.MeetingFolder | None:
        """The meeting being recorded (also while it is stopping), else None."""
        rec = self._recorder
        return rec.folder if rec is not None and self.state.phase in ("recording", "stopping") else None

    def start(self, title: str, terms: list[str] | tuple[str, ...] = ()) -> None:
        if not self.can_start:
            return
        self._set(phase="starting", message="Starting…")

        def run():
            try:
                comps = self._get_components()
                self._recorder = Recorder(self.settings, comps, on_status=self._on_status,
                                          on_error=lambda m: self._notify(m))
                folder = self._recorder.start(title or "meeting", terms)
                self._live_ids.add(folder.meeting_id)
                self._set(phase="recording", message="", last_meeting=folder.meeting_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not start recording")
                self._set(phase="ready", message="")
                self._notify(f"Could not start recording: {exc}")

        threading.Thread(target=run, name="start", daemon=True).start()

    def stop(self, wait: bool = False) -> None:
        if not self.is_recording or self._recorder is None:
            return
        self._set(phase="stopping", message="Finishing live transcript…")
        rec = self._recorder

        def run():
            try:
                folder = rec.stop()
                self._live_ids.discard(folder.meeting_id)
                self._jobs.put(folder)
                self.state.finalize_queue += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("Stop failed")
                self._notify(f"Problem stopping the recording: {exc}")
            finally:
                self._recorder = None
                self._set(phase="ready", message="", recording=None)

        t = threading.Thread(target=run, name="stop", daemon=True)
        t.start()
        if wait:
            t.join()

    def _on_status(self, st: Status) -> None:
        self.state.recording = st
        self._on_change()

    # -- after-Stop pass ------------------------------------------------
    def _finalize_worker(self) -> None:
        from .diarize import SherpaEmbedder, SherpaOfflineDiarizer, final_threads
        from .finalize import finalize_meeting

        while True:
            folder: mt.MeetingFolder = self._jobs.get()
            last = [0.0]
            t_start = time.monotonic()
            speeds: list[float] = []
            length_s: float | None = None
            diarized: list[str] = []
            try:
                meta = folder.read_meta()
                length_s = meeting_length_s(meta)
                rec_settings = meta.get("settings", {})
                diarized = [src for src, on in (("mic", rec_settings.get("diarize_mic", False)),
                                                ("system", rec_settings.get("diarize_system", True))) if on]
                speeds = recent_final_speeds(folder.path.parent, exclude=folder.meeting_id)
            except Exception:  # noqa: BLE001 - the estimate is optional
                log.exception("Could not prepare the time-left estimate for %s", folder.meeting_id)

            def progress(stage: str, frac: float, mid=folder.meeting_id) -> None:
                now = time.monotonic()
                if now - last[0] > 0.5 or frac >= 1.0:
                    last[0] = now
                    overall = pass_fraction(stage, frac, diarized)
                    elapsed = now - t_start
                    self._set(finalizing=f"{stage} {frac:.0%}",
                              finalize=FinalizeProgress(mid, stage, frac, overall, elapsed,
                                                        estimate_remaining_s(overall, elapsed, length_s, speeds)))

            try:
                self._set(finalizing="Identifying speakers",
                          finalize=FinalizeProgress(folder.meeting_id, SPEAKER_STAGE))
                keep = folder.read_meta().get("settings", {}).get("keep_audio", self.settings.keep_audio)
                # Read when the models load, so a meeting started meanwhile gets 1 thread.
                finalize_meeting(
                    folder, lambda: SherpaOfflineDiarizer(
                        threshold=self.settings.final_cluster_threshold, merge_small=False,
                        num_threads=final_threads(self.is_recording)),
                    keep_audio=keep, progress=progress,
                    embedder_factory=lambda: SherpaEmbedder(num_threads=final_threads(self.is_recording)),
                    voice_threshold=self.settings.final_voice_threshold)
                self._notify(f"Final transcript ready: {folder.meeting_id}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Finalizing %s failed", folder.meeting_id)
                try:
                    folder.update_meta(status=mt.STATUS_FAILED, error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
                self._notify(f"Could not finish {folder.meeting_id}: {exc}")
            finally:
                self.state.finalize_queue = max(0, self.state.finalize_queue - 1)
                self._set(finalizing="", finalize=None)

    # -- settings -------------------------------------------------------
    def update_settings(self, new: config.Settings) -> None:
        config.save(replace(new, **self._persisted) if self._persisted else new)
        old = self.settings
        self.settings = replace(new, **self._overrides) if self._overrides else new
        rec = self._recorder
        vocab_changed = list(old.vocabulary) != list(new.vocabulary)
        if self.is_recording and rec is not None and vocab_changed:
            rec.set_vocabulary(settings_terms=list(new.vocabulary))
        if self.is_recording:
            if vocab_changed and _same_apart_from_vocabulary(old, new):
                self._notify("Vocabulary saved. It applies from the next line.")
            else:
                self._notify("Settings saved. They apply from the next recording"
                             + (" (the vocabulary applies from the next line)." if vocab_changed else "."))
        model_changed = (old.whisper_model, old.needs_diarization) != (new.whisper_model, new.needs_diarization)
        if model_changed and not self.is_recording:
            self._set(phase="starting", message="Loading models…")
            self.boot()

    def plan_vocabulary(self, settings_terms: list[str], meeting_terms: list[str] = ()) -> vocab.VocabPlan:
        """What would be sent to Whisper for these terms (for the dialogs). While recording, the
        running meeting's own terms come first when none are given."""
        rec = self._recorder
        if not meeting_terms and self.is_recording and rec is not None:
            meeting_terms = rec.meeting_terms
        comps = self._components
        return vocab.plan(meeting_terms, settings_terms,
                          vocab.counter_for(comps.transcriber) if comps is not None else None)

    # -- helpers --------------------------------------------------------
    def _set(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.state, k, v)
        self._on_change()

    def shutdown(self) -> None:
        if self.is_recording:
            self.stop(wait=True)
