"""Application controller: owns settings, models, the recorder and background jobs.

UI-agnostic; the tray (tray.py) drives it and renders its state.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import config, housekeeping, meeting as mt, models, vocab
from .recorder import Components, Recorder, Status, load_components

log = logging.getLogger(__name__)


@dataclass
class AppState:
    phase: str = "starting"  # starting | downloading | ready | recording | stopping | error
    recording: Status | None = None
    finalizing: str = ""  # "<meeting id>: <stage> 40%"
    finalize_queue: int = 0
    message: str = ""
    last_meeting: str | None = None


def _same_apart_from_vocabulary(a: config.Settings, b: config.Settings) -> bool:
    return {**a.to_dict(), "vocabulary": []} == {**b.to_dict(), "vocabulary": []}


class AppController:
    def __init__(self, on_change: Callable[[], None] | None = None,
                 notify: Callable[[str], None] | None = None):
        self.settings = config.load()
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

            def progress(stage: str, frac: float, mid=folder.meeting_id) -> None:
                now = time.monotonic()
                if now - last[0] > 0.5 or frac >= 1.0:
                    last[0] = now
                    self._set(finalizing=f"{stage} {frac:.0%}")

            try:
                self._set(finalizing="Identifying speakers")
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
                self._set(finalizing="")

    # -- settings -------------------------------------------------------
    def update_settings(self, new: config.Settings) -> None:
        config.save(new)
        old = self.settings
        self.settings = new
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
