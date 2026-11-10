"""System tray app (F3): Start/Stop, a clear recording indicator, settings.

Tk owns the main thread (for dialogs); pystray runs detached. Menu callbacks
from pystray are marshalled onto the Tk thread through a queue.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import time
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageDraw

from . import __version__, marks as marks_mod
from .app import AppController
from .gui import logic
from .replay import Replay, next_step

log = logging.getLogger(__name__)

COLORS = {
    "idle": (110, 118, 129),
    "recording": (220, 38, 38),
    "busy": (217, 119, 6),
    "error": (120, 40, 160),
    "muted": (220, 38, 38),  # the recording icon with a struck-through mic
    "flag": (220, 38, 38),  # the recording icon with a flag, for about a second after a bookmark
}
FLAG_SECONDS = 1.0


def make_icon(kind: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS[kind]
    d.ellipse((2, 2, size - 2, size - 2), fill=color + (255,))
    if kind == "flag":  # a flag in place of the mic: pole and pennant
        white = (255, 255, 255, 255)
        d.line((size * 0.34, size * 0.2, size * 0.34, size * 0.82), fill=white, width=max(3, size // 14))
        d.polygon([(size * 0.34, size * 0.22), (size * 0.8, size * 0.38), (size * 0.34, size * 0.56)], fill=white)
        return img
    # Microphone glyph.
    w = size // 5
    cx = size // 2
    d.rounded_rectangle((cx - w // 2 - 2, size * 0.2, cx + w // 2 + 2, size * 0.58), radius=w // 2,
                        fill=(255, 255, 255, 255))
    d.arc((cx - w - 4, size * 0.32, cx + w + 4, size * 0.68), start=0, end=180, fill=(255, 255, 255, 255),
          width=3)
    d.line((cx, size * 0.68, cx, size * 0.8), fill=(255, 255, 255, 255), width=3)
    if kind == "muted":  # a slash across the mic: dark edge under a white stroke so it shows on both
        d.line((size * 0.22, size * 0.2, size * 0.78, size * 0.82), fill=(60, 10, 10, 255), width=max(6, size // 8))
        d.line((size * 0.22, size * 0.2, size * 0.78, size * 0.82), fill=(255, 255, 255, 255),
               width=max(3, size // 14))
    if kind in ("recording", "muted"):  # the white dot says "recording"; muted adds the slash
        r = size // 7
        d.ellipse((size - 2 * r - 2, 2, size - 2, 2 * r + 2), fill=(255, 255, 255, 255))
    return img


def _single_instance() -> bool:
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\HuddleHelperSingleInstance")
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except AttributeError:
        return True


class TrayApp:
    def __init__(self, replay: Replay | None = None, overrides: dict | None = None,
                 auto_open_window: bool = True, open_latest: bool = False, open_meetings: bool = False):
        import pystray

        from .gui import theme

        theme.make_dpi_aware()  # before the first Tk window, or text is blurry at 125-150% scaling
        self.pystray = pystray
        self.root = tk.Tk()
        self.root.withdraw()
        self.calls: queue.Queue = queue.Queue()
        self.icons = {k: make_icon(k) for k in COLORS}
        self._kind = None
        self._flag_until = 0.0  # monotonic time until which the flag icon shows
        self._hotkeys = None  # hotkeys.HotkeyThread while recording
        self._hotkeys_wanted = False
        self.window = None  # gui.window.MainWindow, built on first use
        self._auto_opened: str | None = None  # meeting id the window was last opened for on Start
        self._auto_open_window = auto_open_window
        self._open_latest = open_latest
        self._open_meetings = open_meetings
        self.replay = replay
        self._replay_started_at: float | None = None
        self.app = AppController(on_change=self._changed, notify=self._notify, overrides=overrides,
                                 capture_factory=replay.capture_factory if replay else None)
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start recording…", self._ui(self.show_start),
                             enabled=lambda item: self.app.can_start, default=True),
            pystray.MenuItem("Stop recording", self._ui(self.stop),
                             enabled=lambda item: self.app.is_recording),
            pystray.MenuItem("Bookmark", pystray.Menu(*[
                pystray.MenuItem(lambda item, k=k: self._bookmark_label(k), self._ui(lambda k=k: self.bookmark(k)),
                                 enabled=lambda item: self.app.is_recording)
                for k in marks_mod.KINDS]), enabled=lambda item: self.app.is_recording),
            pystray.MenuItem("Mute me in the recording", self._ui(self.toggle_mute),
                             checked=lambda item: self.app.mic_muted, enabled=lambda item: self.app.is_recording),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Transcript window", self._ui(self.show_window)),
            pystray.MenuItem("Meetings…", self._ui(self.show_meetings)),
            pystray.MenuItem("Settings…", self._ui(self.show_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._ui(self.quit)),
        )
        self.icon = pystray.Icon("HuddleHelper", self.icons["busy"], "Huddle Helper", menu)

    # -- plumbing -------------------------------------------------------
    def _ui(self, fn):
        """Wrap a menu handler so it runs on the Tk thread."""
        return lambda icon, item: self.calls.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                self.calls.get_nowait()()
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            log.exception("UI action failed")
        self.root.after(100, self._pump)

    def _notify(self, msg: str) -> None:
        log.info("Notify: %s", msg)
        try:
            self.icon.notify(msg, "Huddle Helper")
        except Exception:  # noqa: BLE001
            pass

    # -- state rendering ------------------------------------------------
    def _bookmark_label(self, kind: str) -> str:
        item = next(i for i in logic.bookmark_items(self.app.settings.hotkeys) if i.kind == kind)
        return item.menu_text

    def _status_text(self) -> str:
        st = self.app.state
        if st.phase == "recording" and st.recording:
            r = st.recording
            return logic.recording_status_text(r.elapsed_s, r.segments, r.lag_s, r.marks, r.mic_muted)
        if st.phase == "stopping":
            return "Finishing live transcript…"
        if st.phase in ("starting", "downloading"):
            return st.message or "Starting…"
        if st.phase == "error":
            return st.message[:60] or "Error"
        if st.finalizing:
            more = f" (+{st.finalize_queue - 1} queued)" if st.finalize_queue > 1 else ""
            return f"Finalizing: {st.finalizing}{more}"
        return "Ready"

    def _changed(self) -> None:
        st = self.app.state
        # Called from worker threads: only queue the window opening for the Tk thread.
        if (st.phase == "recording" and st.last_meeting and st.last_meeting != self._auto_opened
                and self._auto_open_window and self.app.settings.open_window_on_start):
            self._auto_opened = st.last_meeting
            self.calls.put(self.show_window)
        if (st.phase == "recording") != self._hotkeys_wanted:  # Start/Stop: register / release the hotkeys
            self._hotkeys_wanted = st.phase == "recording"
            self.calls.put(self._sync_hotkeys)
        if st.phase == "recording":
            kind = "flag" if time.monotonic() < self._flag_until else (
                "muted" if st.recording and st.recording.mic_muted else "recording")
        elif st.phase == "error":
            kind = "error"
        elif st.phase in ("starting", "downloading", "stopping") or st.finalizing:
            kind = "busy"
        else:
            kind = "idle"
        try:
            if kind != self._kind:
                self.icon.icon = self.icons[kind]
                self._kind = kind
            self.icon.title = f"Huddle Helper - {self._status_text()}"[:127]
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 - icon not ready yet
            pass

    # -- bookmarks, mute and hotkeys (WP5) ----------------------------------------------------
    def _sync_hotkeys(self) -> None:
        """Tk thread: hold the global hotkeys exactly while a meeting is being recorded."""
        want = self._hotkeys_wanted
        if want and self._hotkeys is None:
            from .hotkeys import HotkeyThread

            thread = HotkeyThread(self.app.settings.hotkeys, lambda name: self.calls.put(lambda: self.hotkey(name)))
            thread.start()
            self._hotkeys = thread
            problems = [f"{r.combo or thread.bindings[n]} ({self._action_label(n)}): {r.error}"
                        for n, r in thread.failed.items()]
            self.app._set(hotkey_problems=problems)
        elif not want and self._hotkeys is not None:
            self._release_hotkeys()
            self.app._set(hotkey_problems=[])

    def _release_hotkeys(self) -> None:
        thread, self._hotkeys = self._hotkeys, None
        if thread is not None:
            try:
                thread.stop()
            except Exception:  # noqa: BLE001 - registrations die with the thread anyway
                log.exception("Could not stop the hotkey thread")

    @staticmethod
    def _action_label(name: str) -> str:
        from .hotkeys import ACTION_LABELS

        return ACTION_LABELS.get(name, name)

    def hotkey(self, name: str) -> None:
        """A global hotkey was pressed (on the Tk thread, via ``calls``)."""
        result = self.app.hotkey_pressed(name)
        log.info("Hotkey %s: %s", name, result)
        if result.get("action") in ("added", "retyped"):
            self._flash()

    def bookmark(self, kind: str) -> None:
        if self.app.is_recording and self.app.bookmark(kind, "tray").get("action") in ("added", "retyped"):
            self._flash()

    def toggle_mute(self) -> None:
        self.app.toggle_mic_muted()
        self._changed()

    def _flash(self) -> None:
        """Show the flag icon for about a second (no sound: it would be recorded on the system stream)."""
        self._flag_until = time.monotonic() + FLAG_SECONDS
        self._changed()
        self.root.after(int(FLAG_SECONDS * 1000) + 50, self._changed)

    # -- actions --------------------------------------------------------
    def show_start(self) -> None:
        from .ui import StartDialog

        if self.app.can_start:
            StartDialog(self.root, self.app.start, self.app.plan_vocabulary,
                        self.app.settings.vocabulary)

    def stop(self) -> None:
        self.app.stop()

    def _main_window(self):
        from .gui.window import MainWindow

        if self.window is None:
            # post=calls.put: the Meetings view's scan and delete threads reach Tk through the same queue.
            self.window = MainWindow(self.root, self.app, on_stop=self.stop, post=self.calls.put)
        return self.window

    def show_window(self) -> None:
        """Open (or raise) the Transcript window on the meeting being recorded, else the last one."""
        self._main_window().show(self.app.current_folder())

    def show_meetings(self) -> None:
        """Open (or raise) the window on the Meetings list."""
        self._main_window().show_meetings()

    def show_settings(self) -> None:
        from .audio.capture import list_devices
        from .ui import SettingsDialog

        try:
            devices = list_devices()
        except Exception:  # noqa: BLE001
            log.exception("Could not list devices")
            devices = {"inputs": [], "outputs": []}
        SettingsDialog(self.root, self.app.settings, devices, self.app.update_settings,
                       self.app.plan_vocabulary, hotkey_results=self._hotkey_results)

    def _hotkey_results(self, bindings: dict[str, str]) -> tuple[dict, bool]:
        """(registration results, are they live): the recording's own while one is running, else a
        quick try-and-release of these bindings."""
        if self._hotkeys is not None:
            return dict(self._hotkeys.results), True
        from .hotkeys import probe

        return probe(bindings), False

    def quit(self) -> None:
        if self.app.is_recording:
            if not messagebox.askyesno("Huddle Helper", "A meeting is being recorded. Stop recording and quit?\n\n"
                                       "Speaker labels will be refined the next time Huddle Helper starts."):
                return
        if self.app.state.finalizing:
            if not messagebox.askyesno("Huddle Helper", "A final transcript is still being written. Quit anyway?"
                                       "\n\nIt will be finished the next time Huddle Helper starts."):
                return
        self.app.shutdown()
        self._release_hotkeys()
        self.icon.stop()
        self.root.quit()

    # -- unattended replay (dev aid) -------------------------------------------
    def _replay_tick(self) -> None:
        r, st = self.replay, self.app.state
        step = next_step(phase=st.phase, started=r.started, stopped=r.stopped, audio_finished=r.audio_finished,
                         finalizing=bool(st.finalizing), finalize_queue=st.finalize_queue)
        if step == "start":
            r.started = True
            self._replay_started_at = time.monotonic()
            log.info("Replay: starting %s", r.folder)
            self.app.start(r.title)
        elif step == "stop":
            r.stopped = True
            log.info("Replay: audio finished, stopping")
            self.app.stop()
        elif step == "quit" and r.keep_open:
            log.info("Replay: finished (%s); leaving the app open", st.last_meeting)
            print(f"Replay finished: {self.app.settings.resolved_meetings_dir() / (st.last_meeting or '')}",
                  flush=True)
            return
        elif step == "quit":
            log.info("Replay: finished (%s)", st.last_meeting)
            print(f"Replay finished: {self.app.settings.resolved_meetings_dir() / (st.last_meeting or '')}",
                  flush=True)
            self.app.shutdown()
            self.icon.stop()
            self.root.quit()
            return
        elif (r.started and not r.stopped and r.capture is None and st.phase == "ready"
              and time.monotonic() - (self._replay_started_at or 0) > 20):
            log.error("Replay: the recording did not start")
            print("Replay: the recording did not start (see the log).", flush=True)
            self.icon.stop()
            self.root.quit()
            return
        self.root.after(500, self._replay_tick)

    def _purge_daily(self) -> None:
        self.app.purge()
        self.root.after(6 * 3600 * 1000, self._purge_daily)

    def run(self) -> int:
        self.icon.run_detached()
        self._changed()
        self.app.boot()
        self.root.after(100, self._pump)
        if self.replay is not None and self.replay.auto:
            self.root.after(1000, self._replay_tick)
        if self._open_latest:
            self.root.after(500, self.show_window)
        if self._open_meetings:
            self.root.after(500, self.show_meetings)
        self.root.after(6 * 3600 * 1000, self._purge_daily)
        log.info("Huddle Helper %s running in the tray", __version__)
        self.root.mainloop()
        return 0


def run_tray(replay: Replay | None = None, overrides: dict | None = None, auto_open_window: bool = True,
             open_latest: bool = False, open_meetings: bool = False) -> int:
    # A replay is a dev aid and may run next to the real app, so it skips the single-instance check.
    if replay is None and not _single_instance():
        ctypes.windll.user32.MessageBoxW(None, "Huddle Helper is already running in the system tray.",
                                         "Huddle Helper", 0x40)
        return 1
    return TrayApp(replay, overrides, auto_open_window, open_latest, open_meetings).run()
