"""Application controller: owns settings, models, the recorder and background jobs.

UI-agnostic; the tray (tray.py) drives it and renders its state.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import bookmark_types, config, housekeeping, library, marks as marks_mod, meeting as mt, models, playback, tags as tags_mod, vocab
from .final_pass import FinalPass, PassProgress
from .recorder import Components, Recorder, Status, load_components

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayResult:
    """What ``AppController.play_clip`` did: "playing", "confirm" (ask the user, then call again with
    ``confirmed=True``) or "unavailable" (``message`` says why). ``notice`` is a one-time warning to
    show next to a clip that did start."""
    status: str
    message: str = ""
    notice: str = ""


@dataclass
class AppState:
    phase: str = "starting"  # starting | downloading | ready | recording | stopping | error
    recording: Status | None = None
    finalizing: str = ""  # "<stage> 40%" (kept for the tray)
    finalize: PassProgress | None = None  # structured form of the same, None when idle
    finalize_queue: int = 0
    message: str = ""
    last_meeting: str | None = None
    hotkey_problems: list[str] = field(default_factory=list)  # "Ctrl+Alt+B (Bookmark): already in use ..."
    # The voice model is on disk (#55): without it "Several people on my mic" is disabled, in the Start dialog
    # and the Transcript window. Checked at start-up and when a recording starts; never downloaded mid-meeting.
    voice_model_installed: bool = True


REMINDER_ENV = "HUDDLE_HELPER_MUTE_REMINDER_S"  # test seam only: seconds between mute reminders


def _reminder_interval() -> float:
    """10 minutes (``mute_reminder.INTERVAL_S``); the environment variable is for tests and screenshots."""
    from .mute_reminder import INTERVAL_S

    try:
        return float(os.environ[REMINDER_ENV])
    except (KeyError, ValueError):
        return INTERVAL_S


def _same_apart_from_vocabulary(a: config.Settings, b: config.Settings) -> bool:
    return {**a.to_dict(), "vocabulary": []} == {**b.to_dict(), "vocabulary": []}


class AppController:
    def __init__(self, on_change: Callable[[], None] | None = None,
                 notify: Callable[[str], None] | None = None,
                 overrides: dict | None = None, capture_factory: Callable[..., object] | None = None,
                 player: playback.Player | None = None, final_pass: FinalPass | None = None):
        """``overrides`` (dev aids such as ``tray --out``) replace Settings fields for this run only:
        they are never written back to settings.json. ``capture_factory`` replaces the live devices
        (``tray --replay``). ``final_pass`` runs every meeting's final pass; by default one is built
        from the Settings in force when each meeting's pass starts."""
        self.settings = config.load()
        self._overrides = dict(overrides or {})
        self._persisted = {k: getattr(self.settings, k) for k in self._overrides}
        for k, v in self._overrides.items():
            setattr(self.settings, k, v)
        self._capture_factory = capture_factory
        self._final_pass = final_pass
        self.state = AppState()
        self._on_change = on_change or (lambda: None)
        self._notify = notify or (lambda msg: None)
        self._components: Components | None = None
        self._components_key = None
        self._recorder: Recorder | None = None
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._live_ids: set[str] = set()
        self._jobs_lock = threading.Lock()
        self._queued: set[str] = set()  # meeting ids queued for, or in, the after-Stop pass
        self._retry_waiting: list[mt.MeetingFolder] = []  # retries held back while a meeting records
        # Click to hear (WP7): one clip at a time; the output gain is 1.0 unless a dev script lowers it.
        self.player = player or playback.Player()
        self.player.device_name = self.settings.clip_output_device  # "Play clips on" (outputs.py)
        self.player.on_fallback = self._clip_device_missing
        self._fallback_told: set[str] = set()
        self._run_active = False  # a Play from here run is loading or playing
        self._playing: tuple[str, int] | None = None  # (clip key, token) while a clip is loading or playing
        self._play_token = 0
        self._play_lock = threading.Lock()
        self._hp_warned: set[str] = set()  # meetings whose headphone warning was shown
        self._hp_confirmed: set[str] = set()  # meetings where the user confirmed playing despite echo
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
                self._enqueue(mt.MeetingFolder(meetings_dir / meeting_id))
            self.purge()
            self._check_voice_model()
            self._get_components()
            self._set(phase="ready", message="")
        except Exception as exc:  # noqa: BLE001
            log.exception("Startup failed")
            self._set(phase="error", message=f"Startup failed: {exc}")

    def _prepare_models(self) -> None:
        s = self.settings
        # The voice model is installed even with meeting-audio separation off: a meeting can tick
        # "Several people on my mic" at Start (shared mic), which needs it.
        missing = models.missing_models(s.whisper_model, True)
        if missing:
            self._set(phase="downloading", message=f"Downloading models: {', '.join(missing)}")
            self._notify("Downloading speech models (one-time). Recording is available when done.")
            models.install_all(s.whisper_model, True, progress=lambda m: self._set(message=m))
            self._notify("Models installed. Huddle Helper is ready.")
        if s.final_transcribe and not models.whisper_installed(s.final_model):
            # The model for the second transcription after Stop. A failure here must not stop
            # recording: the final pass then keeps the live text.
            try:
                self._set(phase="downloading", message=f"Downloading the {s.final_model} model for final transcripts")
                models.install_whisper(s.final_model, progress=lambda m: self._set(message=m))
            except Exception:  # noqa: BLE001
                log.exception("Could not install the final-transcript model %s", s.final_model)
                self._notify(f"Could not download the {s.final_model} model. Final transcripts will use the live text.")

    def voice_model_installed(self) -> bool:
        """For the Start dialog: is the voice model on disk now (``state.voice_model_installed`` follows)."""
        return self._check_voice_model()

    def _check_voice_model(self) -> bool:
        installed = models.diarization_installed()
        if installed != self.state.voice_model_installed:
            self._set(voice_model_installed=installed)
        return installed

    def _get_components(self, shared_mic: bool = False) -> Components:
        """The loaded models. ``shared_mic``: the meeting about to start needs the voice model even when
        meeting-audio separation is off; components that already hold it (also one loaded part-way through
        an earlier meeting) are reused."""
        s = self.settings
        voice = s.needs_diarization or shared_mic
        key = (s.whisper_model, s.beam_size, s.cpu_threads, voice)
        with self._lock:
            have = self._components_key
            comps = self._components
            # Components without the voice model but with its loader are kept: Recorder.start loads the model
            # into them before recording begins, without loading Whisper again.
            no_voice = voice and getattr(comps, "embedder", None) is None \
                and getattr(comps, "embedder_loader", None) is None
            if comps is None or have is None or have[:3] != key[:3] or no_voice:
                log.info("Loading models for %s", key)
                self._components = load_components(s, shared_mic=shared_mic)
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

    def now_ms(self) -> int | None:
        """The meeting clock of the recording in progress, None when idle."""
        rec = self._recorder
        return rec.now_ms() if rec is not None else None

    # -- bookmarks and mute (WP5) -------------------------------------------------------
    @property
    def mic_muted(self) -> bool:
        rec = self._recorder
        return rec is not None and rec.mic_muted

    def set_mic_muted(self, muted: bool) -> bool:
        """Take the mic out of the recording (or back in). False when nothing is recording."""
        rec = self._recorder
        if rec is None or not self.is_recording:
            return False
        now = rec.set_mic_muted(muted)
        self._on_change()
        return now

    def set_shared_mic(self, on: bool) -> bool:
        """"Several people on my mic" in the Transcript window: open (or close) a shared mic stretch now.
        False when nothing is recording."""
        rec = self._recorder
        if rec is None or not self.is_recording:
            return False
        now = rec.set_shared_mic(on)
        self._on_change()
        return now

    def toggle_mic_muted(self) -> bool:
        return self.set_mic_muted(not self.mic_muted)

    def custom_names(self) -> dict[str, str]:
        """The user-defined bookmark types in Settings, id -> name, in Settings order (L28)."""
        return bookmark_types.names(self.settings.custom_bookmark_types)

    def hotkey_bindings(self, settings: config.Settings | None = None) -> dict[str, str]:
        """Every global hotkey to register while recording: the built-in actions, then the user-defined types
        that have one (their id is the action name)."""
        s = settings or self.settings
        return {**s.hotkeys, **bookmark_types.hotkey_bindings(s.custom_bookmark_types)}

    def _marklog_for(self, folder: mt.MeetingFolder | None) -> tuple[marks_mod.MarkLog | None, bool]:
        """(MarkLog, is the live recording's). The recording's own log keeps the 2 s rule's memory."""
        rec = self._recorder
        live = rec is not None and rec.folder is not None and self.state.phase in ("recording", "stopping")
        if folder is None or (live and folder.path == rec.folder.path):
            return (rec.marklog, True) if live else (None, False)
        return marks_mod.MarkLog(folder), False

    def bookmark(self, kind: str = marks_mod.BOOKMARK, via: str = "hotkey", *,
                 folder: mt.MeetingFolder | None = None, at_ms: int | None = None,
                 seq: int | None = None) -> dict:
        """Add a bookmark. With no ``folder`` it is the meeting being recorded, at the meeting clock now.
        ``via="line"`` (a right-click on a line, any meeting) needs ``at_ms``, the line's start.
        Returns ``{"action": "added" | "ignored" | "retyped" | "none", "id", "kind", "at_ms"}``."""
        log_, live = self._marklog_for(folder)
        if log_ is None:
            return {"action": "none"}
        if live:
            if at_ms is None:
                result = self._recorder.bookmark(kind, via, names=self.custom_names())
            else:
                result = self._recorder.bookmark(kind, via, at_ms, seq, self._wall_time(folder, at_ms),
                                                 names=self.custom_names())
        else:
            if at_ms is None:
                return {"action": "none"}
            result = log_.add(kind, at_ms, via, seq=seq, at=self._wall_time(folder, at_ms))
            self._summarize_marks(folder, self.custom_names())
        if result is None:
            return {"action": "none"}
        self._on_change()
        return result.as_dict()

    def edit_mark(self, folder: mt.MeetingFolder, mark_id: int, *, label: str | None = None,
                  kind: str | None = None, delete: bool = False) -> bool:
        """Change a bookmark's label or type, or delete it (any meeting). False if it does not exist."""
        log_, live = self._marklog_for(folder)
        if log_ is None:
            return False
        done = log_.edit(mark_id, label=label, kind=kind, delete=delete)
        if not live:
            self._summarize_marks(folder, self.custom_names())
        elif kind is not None and self._recorder is not None:
            self._recorder.note_mark_kinds(self.custom_names())
        self._on_change()
        return done

    @staticmethod
    def _summarize_marks(folder: mt.MeetingFolder, names: dict[str, str] | None = None) -> None:
        try:
            folder.update_meta(**marks_mod.summarize(folder, names))
        except (OSError, ValueError):
            log.exception("Could not update the bookmark count of %s", folder.meeting_id)

    @staticmethod
    def _wall_time(folder: mt.MeetingFolder, at_ms: int) -> str | None:
        try:
            return mt.iso(datetime.fromisoformat(folder.read_meta()["started_at"]) + timedelta(milliseconds=at_ms))
        except (OSError, ValueError, KeyError):
            return None

    def hotkey_pressed(self, name: str) -> dict:
        """A global hotkey (``hotkeys.DEFAULTS`` names). Bookmark kinds add a bookmark; ``mute`` toggles."""
        if not self.is_recording:
            return {"action": "none"}
        if name == "mute":
            return {"action": "muted" if self.toggle_mic_muted() else "unmuted"}
        if name in marks_mod.KIND_NAMES or name in self.custom_names():
            return self.bookmark(name, "hotkey")
        return {"action": "none"}

    def start(self, title: str, terms: list[str] | tuple[str, ...] = (),
              attendees: list[str] | tuple[str, ...] = (), start_muted: bool = False,
              shared_mic: bool = False) -> None:
        """Start recording. ``start_muted`` (L24): the mic is out of the recording from the first sample.
        ``shared_mic``: "Several people on my mic" was ticked, so the whole meeting is one shared mic stretch."""
        if not self.can_start:
            return
        self._set(phase="starting", message="Starting…")

        def run():
            try:
                self._check_voice_model()  # the window's box follows it during this meeting
                comps = self._get_components(shared_mic) if shared_mic else self._get_components()
                self._recorder = Recorder(self.settings, comps, on_status=self._on_status,
                                          on_error=lambda m: self._notify(m),
                                          before_reopen=self.player.stop,  # decision 7: nothing open on re-bind
                                          on_mute_reminder=self._mute_reminder,
                                          mute_reminder_s=_reminder_interval())
                folder = self._recorder.start(title or "meeting", terms, attendees=attendees,
                                              start_muted=start_muted, shared_mic=shared_mic)
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
                self._enqueue(folder)
            except Exception as exc:  # noqa: BLE001
                log.exception("Stop failed")
                self._notify(f"Problem stopping the recording: {exc}")
            finally:
                self._recorder = None
                self._set(phase="ready", message="", recording=None)
                self._release_retries()

        t = threading.Thread(target=run, name="stop", daemon=True)
        t.start()
        if wait:
            t.join()

    def _mute_reminder(self, seconds: float) -> None:
        """Every 10 minutes of continuous mute (L24): the app's usual notification path (the tray balloon).
        No sound of our own; the window's mute bar changes from the status."""
        from .mute_reminder import reminder_message

        self._notify(reminder_message(seconds))

    def _on_status(self, st: Status) -> None:
        self.state.recording = st
        self._on_change()

    # -- click to hear (WP7) --------------------------------------------------------------
    @property
    def playing_key(self) -> str | None:
        """Key of the clip that is loading or playing (``playback.Clip.key``); the window polls this."""
        playing = self._playing
        return playing[0] if playing else None

    def _is_live(self, folder: mt.MeetingFolder) -> bool:
        rec = self._recorder
        return (rec is not None and rec.folder is not None and self.state.phase == "recording"
                and rec.folder.path == folder.path)

    def recent_oldest_ms(self, folder: mt.MeetingFolder) -> dict[str, int] | None:
        """For the window: how far back the in-memory audio of the meeting being recorded reaches."""
        return self._recorder.recent_oldest_ms() if self._is_live(folder) else None

    def play_clip(self, folder: mt.MeetingFolder, clip: playback.Clip, *, confirmed: bool = False) -> PlayResult:
        """Play ``clip`` of ``folder``. While that meeting is recording the audio comes from the ring,
        the playback is logged as a window for the drop rule, and the headphone warning applies
        (``playback.headphone_check``); otherwise it is read from the kept audio on a short thread.
        Clicking another clip stops the current one."""
        mid = folder.meeting_id
        if self._is_live(folder):
            rec = self._recorder
            check = playback.headphone_check(True, mid in self._hp_warned, mid in self._hp_confirmed or confirmed,
                                             rec.echo_dropped)
            if check.action == "confirm":
                return PlayResult("confirm", check.message)
            samples = rec.recent_clip(clip)
            if samples is None:
                return PlayResult("unavailable", playback.AFTER_STOP)
            if confirmed:
                self._hp_confirmed.add(mid)
            notice = ""
            if check.action == "warn":
                self._hp_warned.add(mid)
                notice = check.message
            rec.note_playback(len(samples) / playback.SAMPLE_RATE, clip.text)
            self._play(samples, self._claim(clip.key))
            return PlayResult("playing", notice=notice)
        token = self._claim(clip.key)
        threading.Thread(target=self._load_and_play, args=(folder, clip, token), name="clip-read", daemon=True).start()
        return PlayResult("playing")

    @property
    def playing_run(self) -> bool:
        """True while a Play from here run is going (the window follows its highlight)."""
        return self._run_active and self._playing is not None

    def _clip_device_missing(self, name: str) -> None:
        if name not in self._fallback_told:  # once per device name, not once per clip
            self._fallback_told.add(name)
            self._notify(f"The output device '{name}' for clips is not available. Playing on the Windows default output.")

    def play_from(self, folder: mt.MeetingFolder, clips: list[playback.Clip], *, confirmed: bool = False) -> PlayResult:
        """Play ``clips`` (``playsequence.plan_run``) one after the other. Same rules as ``play_clip`` for
        every clip: while the meeting records the audio comes from the ring, each clip opens a playback
        window (plus a joined one, see ``PlaybackWindows.add_run_clip``) just before its sound starts, and
        the headphone warning applies once for the run. The key of the clip being heard is ``playing_key``."""
        if not clips:
            return PlayResult("unavailable", "There is nothing to play from here.")
        mid = folder.meeting_id
        notice = ""
        if self._is_live(folder):
            rec = self._recorder
            check = playback.headphone_check(True, mid in self._hp_warned, mid in self._hp_confirmed or confirmed,
                                             rec.echo_dropped)
            if check.action == "confirm":
                return PlayResult("confirm", check.message)
            if confirmed:
                self._hp_confirmed.add(mid)
            if check.action == "warn":
                self._hp_warned.add(mid)
                notice = check.message
        token = self._claim(clips[0].key)
        self._run_active = True
        self.player.play_run(clips, lambda c, f=folder: self._load_clip(f, c),
                             lambda c, s, f=folder, t=token: self._run_clip_started(f, c, s, t),
                             lambda reason, t=token: self._run_finished(t, reason))
        return PlayResult("playing", notice=notice)

    def _load_clip(self, folder: mt.MeetingFolder, clip: playback.Clip):
        if self._is_live(folder):
            return self._recorder.recent_clip(clip)
        return self._read_clip_files(folder, clip)

    def _run_clip_started(self, folder: mt.MeetingFolder, clip: playback.Clip, samples, token: int) -> None:
        with self._play_lock:
            if self._play_token != token:
                return
            self._playing = (clip.key, token)
        if self._is_live(folder):
            self._recorder.note_playback(len(samples) / playback.SAMPLE_RATE, clip.text, run=True)
        self._on_change()

    def _run_finished(self, token: int, reason: str) -> None:
        rec = self._recorder
        if rec is not None:
            rec.end_playback_run()
        with self._play_lock:
            if self._play_token == token:
                self._run_active = False
        if reason == "unavailable":
            self._notify("Could not read that audio.")
        self._play_finished(token, reason)

    def _claim(self, key: str) -> int:
        """Make ``key`` the current clip (the window shows ■ at once); stops the previous one."""
        self.player.stop()
        with self._play_lock:
            self._play_token += 1
            self._run_active = False
            self._playing = (key, self._play_token)
            token = self._play_token
        self._on_change()
        return token

    def _load_and_play(self, folder: mt.MeetingFolder, clip: playback.Clip, token: int) -> None:
        samples = self._read_clip_files(folder, clip)
        with self._play_lock:
            current = self._playing is not None and self._playing[1] == token
        if not current:
            return  # stopped, or another clip was chosen, while this one was being read
        if samples is None:
            self._play_finished(token, "")
            self._notify("Could not read that audio.")
            return
        self._play(samples, token)

    @staticmethod
    def _read_clip_files(folder: mt.MeetingFolder, clip: playback.Clip):
        if len(clip.sources) == 1:
            return playback.clip_from_files(folder, clip.sources[0], clip.start_ms, clip.end_ms, clip.pad_ms)
        return playback.mixed_from_files(folder, clip)

    def _play(self, samples, token: int) -> None:
        self.player.play(samples, lambda reason, t=token: self._play_finished(t, reason))

    def _play_finished(self, token: int, reason: str) -> None:
        with self._play_lock:
            if self._playing is None or self._playing[1] != token:
                return
            self._playing = None
        if reason.startswith("error"):
            self._notify(f"Could not play the clip: {reason[7:]}")
        self._on_change()

    def stop_playback(self) -> None:
        with self._play_lock:
            self._play_token += 1  # a clip still being read must not start
            self._run_active = False
            self._playing = None
        self.player.stop()
        self._on_change()

    def set_keep_audio(self, folder: mt.MeetingFolder, keep: bool) -> None:
        """Keep this meeting's audio whatever the Keep audio setting says (L20). Written to the meeting's
        own ``settings.keep_audio``; the after-Stop pass re-reads it just before deleting."""
        folder.update_meta_settings(keep_audio=bool(keep))
        self._on_change()

    # -- names (WP8) ------------------------------------------------------------------------
    def tag_speaker(self, folder: mt.MeetingFolder, label: str, name: str | None) -> None:
        """Name every line with the live label ``label``, past and future (``None``: remove the name).
        Appends to speaker_tags.jsonl; the window and the after-Stop pass read it from there. Works for
        the meeting being recorded and, until DONE, for one being processed."""
        mt.append_jsonl(folder.tags_path, tags_mod.tag_speaker_event(label, name))
        self._on_change()

    def tag_line(self, folder: mt.MeetingFolder, line, name: str | None) -> None:
        """Name one live line (``line`` has source, start_ms, end_ms and seq; ``None``: remove its own name)."""
        seq = getattr(line, "seq", None)
        mt.append_jsonl(folder.tags_path, tags_mod.tag_line_event(
            line.source, line.start_ms, line.end_ms, name, seq=seq))
        self._on_change()

    def set_final_name(self, folder: mt.MeetingFolder, final_label: str, name: str | None) -> None:
        """Name a final speaker after DONE: straight into speakers.json, merged with what the file holds
        (the user's names win; names the downstream app set for other speakers stay)."""
        folder.merge_speakers({final_label: name})
        self._on_change()

    # -- final pass ------------------------------------------------------
    def _finalize_worker(self) -> None:
        while True:
            folder: mt.MeetingFolder = self._jobs.get()
            last = [0.0, None]  # when the window was last told, and of which stage

            def progress(p: PassProgress) -> None:
                now = time.monotonic()
                if now - last[0] > 0.5 or p.frac >= 1.0 or p.stage != last[1]:
                    last[0], last[1] = now, p.stage
                    self._set(finalizing=f"{p.label} {p.frac:.0%}", finalize=p)

            try:
                final_pass = self._final_pass or FinalPass(self.settings, busy=lambda: self.is_recording)
                final_pass.run(folder, progress)
                self._notify(f"Final transcript ready: {folder.meeting_id}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Finalizing %s failed", folder.meeting_id)
                try:
                    folder.update_meta(status=mt.STATUS_FAILED, error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
                self._notify(f"Could not finish {folder.meeting_id}: {exc}")
            finally:
                with self._jobs_lock:
                    self._queued.discard(folder.meeting_id)
                self.state.finalize_queue = max(0, self.state.finalize_queue - 1)
                self._set(finalizing="", finalize=None)

    def _enqueue(self, folder: mt.MeetingFolder) -> None:
        with self._jobs_lock:
            self._queued.add(folder.meeting_id)
        self.state.finalize_queue += 1
        self._jobs.put(folder)

    # -- meetings (WP6) ---------------------------------------------------------------------
    def is_busy(self, meeting_id: str) -> bool:
        """Recording, waiting for or in the after-Stop pass, or a retry waiting for a recording to stop:
        such a meeting cannot be deleted or retried."""
        rec = self.current_folder()
        with self._jobs_lock:
            return (meeting_id in self._live_ids or meeting_id in self._queued
                    or any(f.meeting_id == meeting_id for f in self._retry_waiting)
                    or (rec is not None and rec.meeting_id == meeting_id))

    def live_state(self) -> library.LiveState:
        """What the Meetings view needs to label each row (see ``library.status_of``)."""
        rec = self.current_folder()
        fin = self.state.finalize
        with self._jobs_lock:
            return library.LiveState(
                recording=rec.meeting_id if rec is not None else None,
                finalizing=fin.meeting_id if fin is not None else None,
                progress=fin.overall if fin is not None else None,
                queued=frozenset(self._queued),
                waiting=frozenset(f.meeting_id for f in self._retry_waiting))

    def _recording_now(self) -> bool:
        return self._recorder is not None or self.state.phase in ("recording", "stopping")

    def retry_final(self, meeting_id: str) -> str:
        """Run the after-Stop pass again for a meeting whose final pass failed. Returns "started" (queued
        now), "queued" (a meeting is recording: it starts after Stop, so the retry does not slow the
        live transcript) or "refused" (unknown meeting, or it is already busy or finished). The error in
        meta.json is cleared when the job is queued."""
        try:
            folder = library.meeting_folder(self.settings.resolved_meetings_dir(), meeting_id)
            meta = folder.read_meta()
        except (library.DeleteRefused, OSError, ValueError):
            return "refused"
        if self.is_busy(meeting_id) or folder.is_done or meta.get("status") != mt.STATUS_FAILED:
            return "refused"
        with self._jobs_lock:
            if self._recording_now():
                self._retry_waiting.append(folder)
                waiting = True
            else:
                waiting = False
        if waiting:
            self._on_change()
            return "queued"
        self._start_retry(folder)
        return "started"

    def _start_retry(self, folder: mt.MeetingFolder) -> None:
        try:
            folder.drop_meta("error")
        except (OSError, ValueError):
            log.exception("Could not clear the error of %s", folder.meeting_id)
        self._enqueue(folder)
        self._on_change()

    def _release_retries(self) -> None:
        """Stop has completed: queue the retries that were waiting for it."""
        with self._jobs_lock:
            waiting, self._retry_waiting = self._retry_waiting, []
            for folder in waiting:  # busy from here on, without a gap
                self._queued.add(folder.meeting_id)
        for folder in waiting:
            try:
                folder.drop_meta("error")
            except (OSError, ValueError):
                log.exception("Could not clear the error of %s", folder.meeting_id)
            self.state.finalize_queue += 1
            self._jobs.put(folder)
        if waiting:
            self._on_change()

    def delete_meetings(self, ids, *, audio_only: bool = False, permanent: bool = False) -> library.DeleteResult:
        """Delete meetings (or only their kept audio) to the Recycle Bin, or for good. Refuses a meeting that
        is busy. Slow for big folders: call it from a worker thread, not from Tk."""
        meetings_dir = self.settings.resolved_meetings_dir()
        op = library.delete_audio if audio_only else library.delete_meeting
        result = library.DeleteResult()
        hows: set[str] = set()
        for mid in ids:
            try:
                how = op(meetings_dir, mid, is_busy=self.is_busy, permanent=permanent)
            except library.DeleteRefused as exc:
                log.warning("Not deleting %s: %s", mid, exc)
                result.refused.append(mid)
            except OSError as exc:
                log.exception("Could not delete %s", mid)
                result.failed.append((mid, str(exc)))
            else:
                if how == "none":
                    result.skipped.append(mid)
                else:
                    result.done.append(mid)
                    hows.add(how)
        result.how = "deleted" if "deleted" in hows else ("recycled" if hows else "")
        if result.done:
            self._on_change()
        return result

    # -- settings -------------------------------------------------------
    def update_settings(self, new: config.Settings) -> None:
        config.save(replace(new, **self._persisted) if self._persisted else new)
        old = self.settings
        self.settings = replace(new, **self._overrides) if self._overrides else new
        self.player.device_name = self.settings.clip_output_device  # from the next clip, even while recording
        rec = self._recorder
        vocab_changed = list(old.vocabulary) != list(new.vocabulary)
        if self.is_recording and rec is not None and vocab_changed:
            rec.set_vocabulary(settings_terms=list(new.vocabulary))
        if self.is_recording:
            if old.clip_output_device != new.clip_output_device and \
                    {**old.to_dict(), "clip_output_device": None} == {**new.to_dict(), "clip_output_device": None}:
                self._notify("Settings saved. Clips play on the new device from the next clip.")
            elif vocab_changed and _same_apart_from_vocabulary(old, new):
                self._notify("Vocabulary saved. It applies from the next line.")
            else:
                self._notify("Settings saved. They apply from the next recording"
                             + (" (the vocabulary applies from the next line)." if vocab_changed else "."))
        model_changed = ((old.whisper_model, old.needs_diarization, old.final_transcribe and old.final_model)
                         != (new.whisper_model, new.needs_diarization, new.final_transcribe and new.final_model))
        if model_changed and not self.is_recording:
            self._set(phase="starting", message="Loading models…")
            self.boot()

    def add_vocabulary_term(self, raw: str) -> vocab.TermCheck:
        """"Add to vocabulary…" from a line (L37): append one term to the Settings vocabulary through the same path as
        the Settings dialog (``update_settings``: saved to settings.json, and handed to the recording as
        ``Recorder.set_vocabulary`` so it applies from the next utterance). Nothing in the transcript changes.
        Returns the check; the term is added only when ``ok`` (a duplicate or invalid term is not)."""
        check = vocab.check_new_term(raw, self.settings.vocabulary)
        if check.ok:
            self.update_settings(replace(self.settings, vocabulary=[*self.settings.vocabulary, check.term]))
        return check

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
        self.player.stop()
        if self.is_recording:
            self.stop(wait=True)
