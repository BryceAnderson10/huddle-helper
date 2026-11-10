"""Application controller: owns settings, models, the recorder and background jobs.

UI-agnostic; the tray (tray.py) drives it and renders its state.
"""

from __future__ import annotations

import logging
import queue
import statistics
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import config, housekeeping, library, marks as marks_mod, meeting as mt, models, playback, tags as tags_mod, vocab
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
    finalize: FinalizeProgress | None = None  # structured form of the same, None when idle
    finalize_queue: int = 0
    message: str = ""
    last_meeting: str | None = None
    hotkey_problems: list[str] = field(default_factory=list)  # "Ctrl+Alt+B (Bookmark): already in use ..."


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


meeting_length_s = library.meeting_length_s  # how long the meeting was (kept here for the callers of app.*)


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
                 overrides: dict | None = None, capture_factory: Callable[..., object] | None = None,
                 player: playback.Player | None = None):
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
        self._jobs_lock = threading.Lock()
        self._queued: set[str] = set()  # meeting ids queued for, or in, the after-Stop pass
        self._retry_waiting: list[mt.MeetingFolder] = []  # retries held back while a meeting records
        # Click to hear (WP7): one clip at a time; the output gain is 1.0 unless a dev script lowers it.
        self.player = player or playback.Player()
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

    def toggle_mic_muted(self) -> bool:
        return self.set_mic_muted(not self.mic_muted)

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
                result = self._recorder.bookmark(kind, via)
            else:
                result = self._recorder.bookmark(kind, via, at_ms, seq, self._wall_time(folder, at_ms))
        else:
            if at_ms is None:
                return {"action": "none"}
            result = log_.add(kind, at_ms, via, seq=seq, at=self._wall_time(folder, at_ms))
            self._summarize_marks(folder)
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
            self._summarize_marks(folder)
        self._on_change()
        return done

    @staticmethod
    def _summarize_marks(folder: mt.MeetingFolder) -> None:
        try:
            folder.update_meta(**marks_mod.summarize(folder))
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
        if name in marks_mod.KIND_NAMES:
            return self.bookmark(name, "hotkey")
        return {"action": "none"}

    def start(self, title: str, terms: list[str] | tuple[str, ...] = ()) -> None:
        if not self.can_start:
            return
        self._set(phase="starting", message="Starting…")

        def run():
            try:
                comps = self._get_components()
                self._recorder = Recorder(self.settings, comps, on_status=self._on_status,
                                          on_error=lambda m: self._notify(m),
                                          before_reopen=self.player.stop)  # decision 7: nothing open on re-bind
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

    def _claim(self, key: str) -> int:
        """Make ``key`` the current clip (the window shows ■ at once); stops the previous one."""
        self.player.stop()
        with self._play_lock:
            self._play_token += 1
            self._playing = (key, self._play_token)
            token = self._play_token
        self._on_change()
        return token

    def _load_and_play(self, folder: mt.MeetingFolder, clip: playback.Clip, token: int) -> None:
        if len(clip.sources) == 1:
            samples = playback.clip_from_files(folder, clip.sources[0], clip.start_ms, clip.end_ms)
        else:
            samples = playback.mixed_from_files(folder, clip)
        with self._play_lock:
            current = self._playing is not None and self._playing[1] == token
        if not current:
            return  # stopped, or another clip was chosen, while this one was being read
        if samples is None:
            self._play_finished(token, "")
            self._notify("Could not read that audio.")
            return
        self._play(samples, token)

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
        self.player.stop()
        if self.is_recording:
            self.stop(wait=True)
