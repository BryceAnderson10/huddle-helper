"""The Transcript window (plan 4.5): header, notice bar, transcript, status bar.

``MainWindow`` is one ``tk.Toplevel`` kept by ``TrayApp``; closing it only hides it. It reads the
meeting's files through a ``MeetingSession`` on ``after`` timers and reads ``app.state`` for
recording and after-Stop progress. Nothing is pushed into it from other threads, and it never calls
into the recorder.

Timers (all on the Tk thread, all skipped while the window is hidden)
    250 ms  transcript / tags / marks tails, at most 200 events per tick (20 ms while more are waiting)
    500 ms  header, notice bar, status bar from app.state (no file I/O)
    1 s     meta.json / speakers.json if changed; DONE -> swap to the final transcript

Two pages share the window and are swapped in place (plan 4.5): the Transcript page (everything above)
and the Meetings page (``MeetingsView``: the list, filter, delete, retry). The header's "Meetings" button
and the tray's "Meetings…" show the list; opening a meeting there switches back to the transcript.

Bookmarks and mute (WP5): the header's mute toggle and the amber bar, the status bar's Bookmark split
button, the bookmark editor on a mark row, and "Bookmark this line as…" on a line's right-click menu all
call ``AppController`` (``bookmark``, ``edit_mark``, ``toggle_mic_muted``); the changes come back through
the same file tails as everything else.

Click to hear (WP7): every line and bookmark row starts with a play control; a click, ``Space`` on the
selected line, or the right-click menu plays it through ``AppController.play_clip``, ``Esc`` stops. The
speaker bar's chips open a menu with "Hear this voice". Controls that cannot play are greyed out with the
reason in a tooltip (and in the notice bar when clicked). The header's "Keep this meeting's audio" check box
calls ``set_keep_audio``; "Several people on my mic" beside it (while recording) calls ``set_shared_mic``.
The once-per-meeting headphone warning is shown in the notice bar, and the confirmation (echo dedupe already dropped mic lines) is a Yes/No question.

Attendees (WP9a, L13): the numbered strip, the number keys, the confirmation with Undo and the "Who is this?"
bar are drawn here from ``attendee_logic``. The rules are in docs/LIVE_VIEW.md, "Attendees (L13)".
"""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from .. import attendees as att, marks as marks_mod, meeting as mt, playback
from ..session import MeetingSession
from ..transcript_model import LineRow, MarkRow, Reset
from . import attendee_logic as al, logic, theme
from .dialogs import ExportDialog, MarkEditor, NamePicker, VocabTermDialog
from .meetings_view import MeetingsView
from .speaker_bar import SpeakerBar
from .transcript_view import TranscriptView

log = logging.getLogger(__name__)

FAST_MS, FAST_MORE_MS, STATUS_MS, SLOW_MS = 250, 20, 500, 1000
FEEDBACK_S = 3.0  # how long "Decision added" stays in the status bar
TOAST_S = 10.0  # how long "SPEAKER_03 is now Ana · Undo" stays
BANNER_S = 8.0  # how long a one-off notice (headphone warning, "why can't I play this") replaces the notice bar


class Tip:
    """A plain hover tooltip (the mute button explains that the call still hears you)."""

    def __init__(self, widget: tk.Misc, text: str = ""):
        self.widget, self.text, self._job, self._win = widget, text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _enter(self, _event) -> None:
        self._hide()
        if self.text:
            self._job = self.widget.after(500, self._show)

    def _show(self) -> None:
        self._job = None
        win = self._win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        tk.Label(win, text=self.text, justify="left", wraplength=320, background="#ffffe1", relief="solid",
                 borderwidth=1, font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL), padx=6, pady=3).pack()
        win.wm_geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")

    def _hide(self, _event=None) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None
        if self._win is not None:
            self._win.destroy()
            self._win = None


class MainWindow:
    def __init__(self, root: tk.Tk, app, on_stop: Callable[[], None] | None = None,
                 post: Callable[[Callable[[], None]], None] | None = None):
        """``post`` hands a callable from a worker thread to the Tk thread (``TrayApp.calls.put``). Without
        one the window keeps its own inbox and drains it on its 250 ms timer."""
        self.root = root
        self.app = app
        self._on_stop = on_stop
        self._inbox: queue.Queue = queue.Queue()
        self._post = post or self._inbox.put
        self.page = "transcript"
        self.session: MeetingSession | None = None
        self._autoscroll = True
        self._highlight_job: str | None = None
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper")
        self.win.geometry("780x560")
        self.win.minsize(560, 360)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        scale = max(1.0, self.win.winfo_fpixels("1i") / 96.0)
        self._scale = scale
        base = (theme.FONT_FAMILY, theme.FONT_SIZE_SMALL)
        style = ttk.Style(self.win)
        style.configure("Header.TLabel", font=(theme.FONT_FAMILY, 11, "bold"))

        self.tpage = ttk.Frame(self.win)  # the Transcript page: header .. status bar
        self.tpage.pack(fill="both", expand=True)

        # header
        head = ttk.Frame(self.tpage, padding=(10, 8))
        head.pack(fill="x")
        self.dot = tk.Label(head, text="●", fg=theme.IDLE, font=(theme.FONT_FAMILY, 12))
        self.dot.pack(side="left")
        self.head_text = ttk.Label(head, text="No meeting open", style="Header.TLabel")
        self.head_text.pack(side="left", padx=(6, 0))
        self.open_btn = ttk.Button(head, text="Meetings", command=self.show_meetings)
        self.open_btn.pack(side="right")
        self.export_btn = ttk.Button(head, text="Export…", command=self.export, state="disabled")
        self.export_btn.pack(side="right", padx=(0, 6))
        self.mute_btn = ttk.Button(head, text="Mute me in the recording", command=self._toggle_mute)
        self.mute_tip = Tip(self.mute_btn)
        self._mute_shown = False
        # "Keep this meeting's audio" gets a row of its own under the header (while recording or processing):
        # beside the other header buttons it did not fit at the window's minimum width.
        self.keep_row = ttk.Frame(self.tpage, padding=(10, 0, 10, 4))
        self.keep_var = tk.BooleanVar(value=False)
        self.keep_chk = ttk.Checkbutton(self.keep_row, text="Keep this meeting's audio", variable=self.keep_var,
                                        command=self._toggle_keep)
        self.keep_chk.pack(side="left")
        self.keep_hint = ttk.Label(self.keep_row, text="", foreground=theme.MUTED, font=base)
        self.keep_hint.pack(side="left", padx=(8, 0))
        self.keep_tip = Tip(self.keep_chk)
        self._keep_shown = False
        # "Several people on my mic" (shared mic): in the same row, only while the meeting is recording.
        self.shared_var = tk.BooleanVar(value=False)
        self.shared_chk = ttk.Checkbutton(self.keep_row, text="Several people on my mic", variable=self.shared_var,
                                          command=self._toggle_shared)
        self.shared_tip = Tip(self.shared_chk)
        self._shared_shown = False
        self._head_sep = ttk.Separator(self.tpage, orient="horizontal")
        self._head_sep.pack(fill="x")

        # amber mute bar (packed only while muted)
        self.mute_bar = tk.Frame(self.tpage, bg=theme.MUTE_BAR_BG)
        self.mute_bar_label = tk.Label(self.mute_bar, text="MIC MUTED", bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG,
                                       font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL, "bold"))
        self.mute_bar_label.pack(side="left", padx=(10, 8), pady=4)
        self.mute_bar_text = tk.Label(self.mute_bar, text="", bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG,
                                      font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL), anchor="w")
        self.mute_bar_text.pack(side="left", pady=4)
        ttk.Button(self.mute_bar, text="Unmute", command=self._toggle_mute).pack(side="right", padx=10, pady=3)
        self._mute_bar_shown = False

        # notice bar (packed only when there is a notice)
        self.notice = tk.Label(self.tpage, text="", anchor="w", justify="left", font=base,
                               padx=int(10 * scale), pady=int(4 * scale), wraplength=int(740 * scale))
        self._notice_shown = False
        self._banner: tuple[str, float] = ("", 0.0)  # (text, until): replaces the notice bar for a few seconds

        # "Who is this?" prompt / naming confirmation (packed only while there is one; never takes the focus)
        self.prompter = al.WhoPrompter()
        self._toast: tuple[str, al.NamingUndo, Path, float] | None = None
        self.prompt_bar = tk.Frame(self.tpage, bg=theme.CHIP_NEW_BG)
        self.prompt_text = tk.Label(self.prompt_bar, text="", anchor="w", justify="left", font=base,
                                    bg=theme.CHIP_NEW_BG, fg=theme.MUTE_BAR_FG, padx=int(10 * scale),
                                    pady=int(3 * scale), wraplength=int(740 * scale))
        self.prompt_btns = tk.Frame(self.prompt_bar, bg=theme.CHIP_NEW_BG)
        self._prompt_sig: tuple | None = None
        self._prompt_shown = False

        # speaker bar (chips; packed only while the meeting has lines)
        self.speakers = SpeakerBar(self.tpage)
        self.speakers.on_chip = self._chip_menu
        self.chip_menu = tk.Menu(self.speakers, tearoff=False)
        self._speakers_shown = False
        self.strip = tk.Label(self.tpage, text="", anchor="w", justify="left", font=base, bg="white",
                              fg="#4b5563", padx=int(10 * scale), pady=int(2 * scale), wraplength=int(740 * scale))
        self._strip_shown = False

        # status bar (packed before the transcript so it keeps its place when the window is small)
        ttk.Separator(self.tpage, orient="horizontal").pack(side="bottom", fill="x")
        bar = ttk.Frame(self.tpage, padding=(10, 6))
        bar.pack(side="bottom", fill="x")
        self.mic_lbl = ttk.Label(bar, text="", font=base)
        self.call_lbl = ttk.Label(bar, text="", font=base)
        self.stat_lbl = ttk.Label(bar, text="", font=base)
        self._status_layout: tuple = (False, False, False)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop, state="disabled")
        # Bookmark split button: click for a plain bookmark, the arrow for the other types.
        self.bookmark_btn = ttk.Button(bar, text="⚑ Bookmark", command=lambda: self._bookmark("bookmark"))
        self.bookmark_arrow = ttk.Button(bar, text="▾", width=2, command=self._popup_bookmark_menu)
        self.bookmark_menu = tk.Menu(self.bookmark_arrow, tearoff=False)
        self._bar_right: tuple = (False, False)
        self._feedback: tuple[str, float] = ("", 0.0)

        self.view = TranscriptView(self.tpage)
        self.view.pack(fill="both", expand=True)
        self.view.on_edit_mark = self._edit_mark
        self.view.on_delete_mark = self._delete_mark
        self.view.on_bookmark_line = self._bookmark_line
        self.view.bookmark_items = lambda: logic.bookmark_items(self._hotkeys(), self._settings_kinds())
        self.view.on_name_line = self._name_line
        self.view.line_name_menu = self._line_menu
        self.view.on_play = self._play_row
        self.view.on_hear_voice = self._hear_voice
        self.view.on_play_from = self._play_from
        self.view.on_add_vocab = self._add_vocab
        self.view.voice_state = self._voice_state
        self.win.bind("<Escape>", lambda e: self.app.stop_playback())
        self.win.bind("<Key>", self._on_key, add="+")  # 1-9 name the latest voice (attendees)

        self.meetings = MeetingsView(self.win, app, post=self._post, on_open=self.open_meeting,
                                     on_back=self.show_transcript, has_session=lambda: self.session is not None,
                                     on_deleted=self._meetings_deleted)

        self._after: dict[str, str] = {}
        self._schedule("fast", FAST_MS, self._tick_fast)
        self._schedule("status", STATUS_MS, self._tick_status)
        self._schedule("slow", SLOW_MS, self._tick_slow)
        self.win.withdraw()

    # -- visibility -----------------------------------------------------------------
    @property
    def visible(self) -> bool:
        try:
            return self.win.state() not in ("withdrawn", "iconic")
        except tk.TclError:
            return False

    def show(self, folder: mt.MeetingFolder | None = None) -> None:
        """Bring the window up on ``folder``; with none, on the meeting being recorded, else the one
        being finalized, else the last one, else the newest on disk."""
        if folder is None and self.session is not None and self.app.current_folder() is None:
            folder = self.session.folder  # keep what is open
        if folder is None:
            st = self.app.state
            folder = logic.default_meeting(
                self.app.settings.resolved_meetings_dir(), recording=self.app.current_folder(),
                finalizing_id=st.finalize.meeting_id if st.finalize else None, last_id=st.last_meeting)
        if folder is not None and (self.session is None or self.session.folder.path != folder.path):
            self.open_meeting(folder)
        elif self.page != "transcript":
            self._show_page("transcript")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._refresh_all()

    def hide(self) -> None:
        self.win.withdraw()

    def destroy(self) -> None:
        for job in self._after.values():
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
        self.win.destroy()

    # -- meeting ----------------------------------------------------------------------
    def show_meetings(self) -> None:
        """Bring the window up on the Meetings page."""
        self._show_page("meetings")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def show_transcript(self) -> None:
        if self.session is not None:
            self._show_page("transcript")

    def _show_page(self, page: str) -> None:
        if page == self.page:
            return
        self.page = page
        if page == "meetings":
            self.tpage.pack_forget()
            self.meetings.pack(fill="both", expand=True)
            self.win.title("Meetings — Huddle Helper")
            self.meetings.activate()
        else:
            self.meetings.pack_forget()
            self.tpage.pack(fill="both", expand=True)
            self._refresh_all()

    def _meetings_deleted(self, ids: list[str]) -> None:
        """Meetings were deleted: forget the open one if it was among them."""
        if self.session is not None and self.session.meeting_id in ids:
            self.session = None
            self.view.clear()
            self.export_btn.configure(state="disabled")
            self._cancel_highlight()

    def open_meeting(self, folder: mt.MeetingFolder) -> None:
        self._show_page("transcript")
        if self.session is not None and self.session.folder.path != folder.path:
            self.app.stop_playback()  # a clip of the meeting just left would play on, unlabelled
        self.view.clear()
        self.session = MeetingSession(folder)
        self.prompter.reset()
        self._toast = None
        st = self.app.state
        phase = logic.meeting_phase(folder.meeting_id, self.session.model, st)
        # Follow the end while lines are still arriving; a finished meeting opens at its top.
        self._autoscroll = phase in (logic.RECORDING, logic.STOPPING, logic.PROCESSING)
        if self._autoscroll:
            self.view.jump_to_end()
        else:
            self.view.scroll_to_top()
        self.export_btn.configure(state="normal")
        self._cancel_highlight()
        self.view.apply([Reset()], self.session.model, self._autoscroll)  # gap rows already in meta
        self._apply(self.session.poll())
        self._refresh_all()

    def export(self) -> None:
        if self.session is not None:
            ExportDialog(self.win, self.session.model)

    def _stop(self) -> None:
        if self._on_stop is not None:
            self._on_stop()

    # -- bookmarks and mute (WP5) ------------------------------------------------------------
    def _hotkeys(self) -> dict[str, str]:
        """Every hotkey by action name, the user-defined bookmark types' too (keyed by their id)."""
        bindings = getattr(self.app, "hotkey_bindings", None)
        return dict(bindings() if bindings else getattr(self.app.settings, "hotkeys", None) or {})

    def _settings_kinds(self) -> dict[str, str]:
        """The user-defined bookmark types in Settings now, id -> name: what the menus offer for new bookmarks."""
        names = getattr(self.app, "custom_names", None)
        return dict(names()) if names else {}

    def _custom_kinds(self) -> dict[str, str]:
        """Names of every user-defined type that can show up here: the meeting's own (``meta.json``
        ``mark_kinds``, so a type removed from Settings keeps its name), then Settings' types."""
        own = {k: v for k, v in (self.session.model.mark_kinds if self.session else {}).items()
               if k not in marks_mod.KIND_NAMES}
        return {**self._settings_kinds(), **own}

    def _say(self, text: str) -> None:
        self._feedback = (text, time.monotonic() + FEEDBACK_S)

    def _poll_now(self) -> None:
        if self.session is not None:
            self._apply(self.session.poll())

    def _bookmark(self, kind: str) -> None:
        result = self.app.bookmark(kind, "button")
        self._say(logic.bookmark_feedback(result, self._custom_kinds()))
        self._poll_now()
        self._refresh_all()

    def _bookmark_line(self, row: LineRow, kind: str) -> None:
        if self.session is None:
            return
        result = self.app.bookmark(kind, "line", folder=self.session.folder, at_ms=row.start_ms, seq=row.seq)
        self._say(logic.bookmark_feedback(result, self._custom_kinds()))
        self._poll_now()

    def _edit_mark(self, row: MarkRow) -> None:
        if self.session is None:
            return
        folder = self.session.folder
        MarkEditor(self.win, self.session.model, row,
                   on_save=lambda changes: self._mark_edited(folder, row, **changes),
                   on_delete=lambda: self._delete_mark(row), custom=self._custom_kinds())

    def _mark_edited(self, folder, row: MarkRow, **changes) -> None:
        self.app.edit_mark(folder, row.id, **changes)
        self._poll_now()

    def _delete_mark(self, row: MarkRow) -> None:
        if self.session is not None:
            self.app.edit_mark(self.session.folder, row.id, delete=True)
            self._poll_now()

    def _toggle_mute(self) -> None:
        self.app.toggle_mic_muted()
        self._refresh_all()

    # -- click to hear (WP7) ----------------------------------------------------------------------
    def _banner_view(self) -> logic.NoticeView | None:
        text, until = self._banner
        return logic.NoticeView(text, "warn") if text and time.monotonic() < until else None

    def _say_banner(self, text: str, seconds: float = BANNER_S) -> None:
        self._banner = (text, time.monotonic() + seconds)
        self._refresh_all()

    def audio_context(self) -> playback.AudioContext:
        """Where the displayed meeting's audio can be read from right now."""
        s = self.session
        folder, model = s.folder, s.model
        phase = logic.meeting_phase(s.meeting_id, model, self.app.state)
        present = [src for src in ("mic", "system") if folder.audio_file(src).exists()]
        return playback.audio_context(
            recording=phase == logic.RECORDING, stopping=phase == logic.STOPPING, done=folder.is_done,
            meta=model.meta, present=present, oldest_ms=self.app.recent_oldest_ms(folder))

    def _refresh_play(self) -> None:
        if self.session is not None:
            self.view.set_audio(self.audio_context(), self.app.playing_key, self.app.playing_run)

    def _play_row(self, row) -> None:
        """The play control of a line or bookmark row (also Space on the selected line)."""
        if self.session is None:
            return
        clip = logic.row_clip(self.session.model, row)
        if clip is None:
            return
        if self.app.playing_key == clip.key:
            self.app.stop_playback()
            return
        state = self.audio_context().clip_state(clip)
        if not state.enabled:
            self._say_banner(state.reason)
            return
        self._start_clip(clip)

    def _play_from(self, row) -> None:
        """"Play from here" in a line's menu: that line and the following ones, one after the other (L19)."""
        if self.session is None:
            return
        ctx = self.audio_context()
        clips = logic.run_clips(self.session.model, row, ctx)
        if not clips:
            self._say_banner(logic.row_play_state(ctx, row).reason or "There is nothing to play from here.")
            return
        self._start_run(clips)

    def _start_run(self, clips: list[playback.Clip], confirmed: bool = False) -> None:
        result = self.app.play_from(self.session.folder, clips, confirmed=confirmed)
        if result.status == "confirm":
            if messagebox.askyesno("Play this clip?", result.message, parent=self.win, default="no"):
                self._start_run(clips, confirmed=True)
            return
        if result.status != "playing":
            self._say_banner(result.message)
            return
        if result.notice:
            self._say_banner(result.notice)
        self.view.set_playing(self.app.playing_key, self.app.playing_run)

    def _start_clip(self, clip: playback.Clip, confirmed: bool = False) -> None:
        result = self.app.play_clip(self.session.folder, clip, confirmed=confirmed)
        if result.status == "confirm":
            if messagebox.askyesno("Play this clip?", result.message, parent=self.win, default="no"):
                self._start_clip(clip, confirmed=True)
            return
        if result.status != "playing":
            self._say_banner(result.message)
            return
        if result.notice:
            self._say_banner(result.notice)
        self.view.set_playing(self.app.playing_key)

    def _voice_state(self, person_key: str) -> tuple[bool, str]:
        if self.session is None:
            return False, ""
        row, why = logic.voice_sample(self.session.model, person_key, self.audio_context())
        return row is not None, why

    def _hear_voice(self, person_key: str) -> None:
        if self.session is None:
            return
        row, why = logic.voice_sample(self.session.model, person_key, self.audio_context())
        if row is None:
            self._say_banner(why)
            return
        clip = playback.line_clip(row)
        if self.app.playing_key == clip.key:
            self.app.stop_playback()
        else:
            self._start_clip(clip)

    def _chip_menu(self, person_key: str, x_root: int, y_root: int) -> None:
        """A speaker chip was clicked: name it, remove its name, hear it."""
        ok, why = self._voice_state(person_key)
        menu = self.chip_menu
        menu.delete(0, "end")
        chip = next((c for c in logic.speaker_chips(self.session.model) if c.person_key == person_key), None)             if self.session is not None else None
        named = chip is not None and not chip.unnamed and chip.display not in logic.KNOWN_LABELS             and chip.display not in chip.labels
        menu.add_command(label="Name…" if not named else "Rename…", command=lambda: self.name_person(person_key))
        if named:
            menu.add_command(label="Remove name", command=lambda: self._apply_person_name(person_key, None))
        menu.add_separator()
        menu.add_command(label="Hear this voice", command=lambda: self._hear_voice(person_key),
                         state="normal" if ok else "disabled")
        if not ok and why:
            menu.add_command(label=logic.short_name(why, 60), state="disabled")
        menu.tk_popup(x_root, y_root)

    # -- naming (WP8) -----------------------------------------------------------------------------
    def name_person(self, person_key: str) -> None:
        """Ask for a name for a chip and apply it to all of that person's lines, past and future."""
        if self.session is None:
            return
        m = self.session.model
        chip = next((c for c in logic.speaker_chips(m) if c.person_key == person_key), None)
        shown = chip.display if chip else person_key
        att_names = self._attendees()
        NamePicker(self.win, "Who is this?", f"Who is {shown}? The name is used for every line of this voice.",
                   al.pick_names(m, att_names, exclude=person_key), "" if chip is None or chip.unnamed else shown,
                   on_done=lambda name: self._apply_person_name(person_key, name), heading=self._pick_heading())

    def _apply_person_name(self, person_key: str, name: str | None) -> None:
        s = self.session
        if s is None:
            return
        plan = logic.name_plan(s.model, person_key)
        if s.model.final:  # naming after DONE goes straight into speakers.json
            for label in plan.labels:
                self.app.set_final_name(s.folder, label, name)
            self._tick_slow()
        else:
            for label in plan.labels:
                self.app.tag_speaker(s.folder, label, name)
            for row in plan.lines:
                self.app.tag_line(s.folder, row, name)
            self._poll_now()
        self._refresh_all()

    def _name_line(self, row: LineRow, name: str | None) -> None:
        """"This line is…": ``name`` None asks, "" clears the line's own name."""
        s = self.session
        if s is None or s.model.final:
            return
        if name is None:
            note = logic.short_line_note(row)
            NamePicker(self.win, "This line is…", f"Who said this line? Only this line changes.{chr(10)}“{logic.short_name(row.text, 60)}”"
                       + (f"{chr(10)}{note}" if note else ""),
                       al.pick_names(s.model, self._attendees(), exclude=row.name), "",
                       on_done=lambda n: self._name_line(row, n), heading=self._pick_heading())
            return
        self.app.tag_line(s.folder, row, name or None)
        self._poll_now()
        self._refresh_all()

    # -- vocabulary from a line (L37) ---------------------------------------------------------------------
    def _add_vocab(self, row: LineRow, selected: str) -> None:
        """"Add to vocabulary…" on a line: ask for the term (the selected words prefilled), add it to Settings.
        The transcript is not edited."""
        VocabTermDialog(self.win, selected, existing=lambda: list(self.app.settings.vocabulary),
                        plan=self.app.plan_vocabulary, on_done=self.add_vocabulary_term)

    def add_vocabulary_term(self, term: str) -> None:
        check = self.app.add_vocabulary_term(term)
        if not check.ok:
            self._say(check.message)
            return
        recording = self.app.is_recording
        self._say(f"“{check.term}” added to the vocabulary"
                  + (". It applies from the next line." if recording else ". It applies to your next recording."))
        if not check.fits:
            self._say_banner(check.message)

    # -- attendees (WP9a, L13) -------------------------------------------------------------------
    def _attendees(self) -> list[str]:
        return att.from_meta(self.session.model.meta) if self.session is not None else []

    def _pick_heading(self) -> str:
        return "On the call, and used in this meeting" if self._attendees() else "Used in this meeting"

    def _line_menu(self, row: LineRow) -> logic.LineNameMenu:
        m = self.session.model
        nm = logic.line_name_menu(m, row)
        if not nm.enabled:
            return nm
        return replace(nm, choices=tuple(al.pick_names(m, self._attendees(), exclude=row.name)))

    def _live_naming(self) -> bool:
        """The live view of a meeting being recorded or processed: where keys and prompts work."""
        s = self.session
        return (s is not None and self.page == "transcript" and not s.model.final
                and logic.meeting_phase(s.meeting_id, s.model, self.app.state)
                in (logic.RECORDING, logic.STOPPING, logic.PROCESSING))

    def _on_key(self, event) -> str | None:
        """Keys 1-9 with the window focused: name the latest call voice as attendee N. Does nothing in a text
        box, in the final view, on the Meetings page or without attendees."""
        n = al.digit_of(event.char, event.state)
        if n is None or self.session is None or not self._live_naming():
            return None
        try:
            w = self.win.focus_get()
            cls = w.winfo_class() if w is not None else ""
            state = str(w.cget("state")) if cls == "Text" else "normal"
        except (KeyError, tk.TclError):
            cls, state = "", "normal"
        if al.focus_blocks_keys(cls, state):
            return None
        names = self._attendees()
        if not names:
            return None
        if n > len(names):
            self._say(f"No attendee {n} (the list has {len(names)})")
            return "break"
        self.name_latest_as(names[n - 1])
        return "break"

    def name_latest_as(self, name: str) -> None:
        """Name the voice the keys point at (``attendee_logic.key_voice``) as ``name``."""
        s = self.session
        target = al.key_voice(s.model, self.prompter.current) if s is not None else None
        if target is None:
            self._say("No line from the call to name yet")
            return
        if target[1] == name:
            self._say(f"{name} is already named")
            return
        self._name_with_undo(target[0], target[1], name)

    def _name_with_undo(self, person_key: str, before: str, name: str) -> None:
        s = self.session
        undo = al.capture_undo(s.model, person_key)
        self._apply_person_name(person_key, name)
        self._toast = (al.confirmation_text(before, name), undo, s.folder.path, time.monotonic() + TOAST_S)
        self._refresh_all()

    def _undo_naming(self) -> None:
        t, self._toast = self._toast, None
        s = self.session
        if t is None or s is None or s.folder.path != t[2]:
            return
        for label, prev in t[1].labels:
            self.app.tag_speaker(s.folder, label, prev)
        for row, prev in t[1].lines:
            self.app.tag_line(s.folder, row, prev)
        self._poll_now()
        self._say("Naming undone")

    def _prompt_action(self, action: str) -> None:
        voice = self.prompter.current
        if action == "undo":
            self._undo_naming()
        elif action == "dismiss":
            if self._toast is not None:
                self._toast = None
            else:
                self.prompter.dismiss()
        elif voice is not None and action == "other":
            NamePicker(self.win, "Who is this?", f"Who is {voice.display}? The name is used for every line of this voice.",
                       al.pick_names(self.session.model, self._attendees()), "",
                       on_done=lambda name, v=voice: self._name_with_undo(v.person_key, v.display, name),
                       heading=self._pick_heading())
        elif voice is not None and action.startswith("pick:"):
            names = self._attendees()
            n = int(action.split(":")[1])
            if 1 <= n <= len(names):
                self._name_with_undo(voice.person_key, voice.display, names[n - 1])
        self._refresh_all()

    def _prompt_view(self) -> al.PromptView | None:
        s = self.session
        voice = self.prompter.update(s.model, self._live_naming(), self._attendees()) if s is not None else None
        if self._toast is not None and (s is None or time.monotonic() >= self._toast[3] or s.folder.path != self._toast[2]):
            self._toast = None
        if self._toast is not None:
            return al.undo_view(self._toast[0] + " ·")
        if voice is not None:
            return al.prompt_view(voice, self._attendees(), al.used_names(s.model))
        return None

    def _set_prompt(self) -> None:
        v = self._prompt_view()
        if v is None:
            if self._prompt_shown:
                self.prompt_bar.pack_forget()
                self._prompt_shown = False
            return
        sig = (v.text, v.buttons, v.inline)
        if sig != self._prompt_sig:
            self._prompt_sig = sig
            self.prompt_text.configure(text=v.text)
            self.prompt_text.pack_forget()
            self.prompt_btns.pack_forget()
            side = "left" if v.inline else "top"
            self.prompt_text.pack(side=side, fill="none" if v.inline else "x")
            self.prompt_btns.pack(side=side, fill="none" if v.inline else "x", padx=int(10 * self._scale),
                                  pady=(0 if v.inline else 0, int(4 * self._scale)))
            for child in self.prompt_btns.winfo_children():
                child.destroy()
            for b in v.buttons:  # takefocus off: the bar must not pull the keyboard focus from the transcript
                ttk.Button(self.prompt_btns, text=b.label, takefocus=False,
                           command=lambda a=b.action: self._prompt_action(a)).pack(side="left", padx=(0, 6))
        if not self._prompt_shown:
            self.prompt_bar.pack(fill="x", before=self._below(self.prompt_bar))
            self._prompt_shown = True

    def _set_strip(self) -> None:
        s = self.session
        names = self._attendees()
        text = al.strip_text(s.model, names, self.prompter.current) if s is not None and names and self._live_naming() else ""
        if not text:
            if self._strip_shown:
                self.strip.pack_forget()
                self._strip_shown = False
            return
        if self.strip.cget("text") != text:
            self.strip.configure(text=text)
        if not self._strip_shown:
            self.strip.pack(fill="x", before=self.view)
            self._strip_shown = True

    def _below(self, widget) -> tk.Misc:
        """The first shown bar under ``widget`` in the stack (mute bar, notice, prompt, speakers, strip), else the transcript."""
        stack = [(self.mute_bar, self._mute_bar_shown), (self.notice, self._notice_shown),
                 (self.prompt_bar, self._prompt_shown), (self.speakers, self._speakers_shown),
                 (self.strip, self._strip_shown)]
        i = next(k for k, (w, _) in enumerate(stack) if w is widget)
        return next((w for w, on in stack[i + 1:] if on), self.view)

    def _set_speakers(self, model) -> None:
        chips = logic.speaker_chips(model) if model is not None else []
        if chips and not self._speakers_shown:
            self.speakers.pack(fill="x", before=self._below(self.speakers))
            self._speakers_shown = True
        elif not chips and self._speakers_shown:
            self.speakers.pack_forget()
            self._speakers_shown = False
        if chips:
            colors = {r.person_key: r for r in model.rows if isinstance(r, LineRow)}
            short = {c.person_key for c in chips if c.short}
            self.speakers.set_chips(chips, lambda key: theme.dim(self.view.color_of(colors[key])) if key in short
                                    else self.view.color_of(colors[key]))

    def _set_keep(self, v: logic.KeepAudioView) -> None:
        if v.visible:
            if self.keep_var.get() != v.checked:
                self.keep_var.set(v.checked)
            self.keep_tip.text = v.tooltip
            hint = ("The audio is kept, so lines can be played later." if v.checked
                    else "Otherwise the audio is deleted when the final transcript is ready.")
            if self.keep_hint.cget("text") != hint:
                self.keep_hint.configure(text=hint)
            if not self._keep_shown:
                self.keep_row.pack(fill="x", after=self._head_sep)
                self._keep_shown = True
        elif self._keep_shown:
            self.keep_row.pack_forget()
            self._keep_shown = False

    def _set_shared(self, v: logic.SharedMicView) -> None:
        if v.visible:
            if self.shared_var.get() != v.checked:
                self.shared_var.set(v.checked)
            self.shared_tip.text = v.tooltip
            want = ["!disabled"] if v.enabled else ["disabled"]
            if self.shared_chk.instate(["disabled"]) == v.enabled:
                self.shared_chk.state(want)
            if not self._shared_shown:
                self.shared_chk.pack(side="right")
                self._shared_shown = True
        elif self._shared_shown:
            self.shared_chk.pack_forget()
            self._shared_shown = False

    def _toggle_shared(self) -> None:
        if self.session is not None:
            self.app.set_shared_mic(bool(self.shared_var.get()))
            self._tick_slow()  # read the new meta now: the box, the divider and the mute tooltip follow it

    def _toggle_keep(self) -> None:
        if self.session is not None:
            self.app.set_keep_audio(self.session.folder, bool(self.keep_var.get()))
            self._tick_slow()  # read the new meta now, so the box does not flip back until the next second

    # -- timers --------------------------------------------------------------------------
    def _schedule(self, name: str, ms: int, fn) -> None:
        def run():
            nxt = ms
            try:
                if self.visible:
                    nxt = fn() or ms
            except Exception:  # noqa: BLE001 - a bug must not stop the timer chain
                log.exception("Transcript window timer %s failed", name)
            try:
                self._after[name] = self.win.after(nxt, run)
            except tk.TclError:
                pass  # window destroyed

        self._after[name] = self.win.after(ms, run)

    def _drain_inbox(self) -> None:
        try:
            while True:
                self._inbox.get_nowait()()
        except queue.Empty:
            pass

    def _tick_fast(self) -> int:
        self._drain_inbox()
        s = self.session
        if s is None:
            return FAST_MS
        self.view.set_playing(self.app.playing_key, self.app.playing_run)
        ops = s.poll()
        self._apply(ops)
        return FAST_MORE_MS if s.has_more else FAST_MS

    def _tick_slow(self) -> int:
        s = self.session
        if s is None:
            return SLOW_MS
        slow = s.poll_slow()
        self._apply(slow.ops)
        if slow.swapped:
            self._after_swap(slow.changed)
        return SLOW_MS

    def _tick_status(self) -> int:
        if self.page == "meetings":
            self.meetings.tick()
            return STATUS_MS
        self._refresh_all()
        return STATUS_MS

    def _apply(self, ops) -> None:
        if ops and self.session is not None:
            self.view.set_short(logic.short_speakers(self.session.model))  # before the rows are drawn (L40)
            self.view.apply(ops, self.session.model, self._autoscroll)
        self._placeholder()

    def _placeholder(self) -> None:
        s = self.session
        if s is None:
            self.view.set_placeholder("No meeting open.\n\nChoose “Meetings”, or start a recording.")
        elif not s.model.rows:
            phase = logic.meeting_phase(s.meeting_id, s.model, self.app.state)
            self.view.set_placeholder("Listening… lines appear a second or two after someone pauses."
                                      if phase == logic.RECORDING else
                                      "No transcript lines in this meeting." if not s.has_more else None)
        else:
            self.view.set_placeholder(None)

    # -- final swap ---------------------------------------------------------------------------
    def _after_swap(self, changed) -> None:
        """The view just redrew from the final transcript: highlight changed lines for 10 s."""
        self._cancel_highlight()
        if changed and self.view.highlight(changed):
            self._highlight_job = self.win.after(theme.HIGHLIGHT_SECONDS * 1000, self._cancel_highlight)

    def _cancel_highlight(self) -> None:
        if self._highlight_job is not None:
            try:
                self.win.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
            self._highlight_job = None
        self.view.clear_highlight()

    # -- header / notice / status -----------------------------------------------------------------
    def _refresh_all(self) -> None:
        s = self.session
        state = self.app.state
        if s is None:
            self.dot.configure(fg=theme.IDLE)
            self.head_text.configure(text="No meeting open")
            self._set_mute(None, None)
            self._set_keep(logic.KeepAudioView(False))
            self._set_shared(logic.SharedMicView(False))
            self._set_prompt()
            self._set_speakers(None)
            self._set_strip()
            self._set_notice(None)
            self._set_status(logic.StatusView(None, None, "", False, False))
            self._placeholder()
            return
        mid, model = s.meeting_id, s.model
        h = logic.header_view(mid, model, state)
        self.dot.configure(fg=theme.DOT_COLORS.get(h.dot, theme.IDLE))
        if self.head_text.cget("text") != h.text:
            self.head_text.configure(text=h.text)
            self.head_text.update_idletasks()  # re-measure now: a busy main loop was seen to leave it clipped
        self.win.title(f"{model.title} — Huddle Helper")
        self._set_mute(logic.mute_button_view(mid, model, state, self._hotkeys().get("mute", "")),
                       logic.mute_bar_view(mid, model, state))
        self._set_notice(self._banner_view() or logic.notice_view(mid, model, state))
        self._set_status(logic.status_view(mid, model, state))
        self._set_keep(logic.keep_audio_view(mid, model, state))
        self._set_shared(logic.shared_mic_view(mid, model, state))
        self._set_prompt()
        self.view.set_short(logic.short_speakers(model))  # a name given in the final view can merge two voices
        self._set_speakers(model)
        self._set_strip()
        self._refresh_play()

    def _set_mute(self, button: logic.MuteButtonView | None, bar: logic.MuteBarView | None) -> None:
        """The header toggle and the amber bar (the bar sits above the notice bar)."""
        show = button is not None and button.visible
        if show:
            if self.mute_btn.cget("text") != button.text:
                self.mute_btn.configure(text=button.text)
            self.mute_tip.text = button.tooltip
            if not self._mute_shown:
                self.mute_btn.pack(side="right", padx=(0, 6))
        elif self._mute_shown:
            self.mute_btn.pack_forget()
        self._mute_shown = show
        if bar is not None:
            bg, fg = ((theme.MUTE_REMIND_BG, theme.MUTE_REMIND_FG) if bar.emphasis
                      else (theme.MUTE_BAR_BG, theme.MUTE_BAR_FG))
            if self.mute_bar.cget("bg") != bg:
                self.mute_bar.configure(bg=bg)
                for w in (self.mute_bar_label, self.mute_bar_text):
                    w.configure(bg=bg, fg=fg)
            if self.mute_bar_text.cget("text") != bar.text:
                self.mute_bar_text.configure(text=bar.text)
            if not self._mute_bar_shown:
                self.mute_bar.pack(fill="x", before=self._below(self.mute_bar))
                self._mute_bar_shown = True
        elif self._mute_bar_shown:
            self.mute_bar.pack_forget()
            self._mute_bar_shown = False

    def _set_notice(self, n: logic.NoticeView | None) -> None:
        if n is None:
            if self._notice_shown:
                self.notice.pack_forget()
                self._notice_shown = False
            return
        bg, fg = {"error": (theme.FAILED_BG, theme.FAILED_FG), "warn": (theme.WARN_BG, theme.WARN_FG)}.get(
            n.level, (theme.NOTICE_BG, theme.NOTICE_FG))
        if self.notice.cget("text") != n.text or self.notice.cget("bg") != bg:
            self.notice.configure(text=n.text, bg=bg, fg=fg)
        if not self._notice_shown:
            self.notice.pack(fill="x", before=self._below(self.notice))
            self._notice_shown = True

    def _set_status(self, v: logic.StatusView) -> None:
        """Mic/Call meters and the text. Labels with nothing to say are not packed, so the text sits
        at the left edge instead of after two empty gaps."""
        text, until = self._feedback
        extra = text if text and time.monotonic() < until else ""
        joined = f"{v.text}   ·   {extra}" if v.text and extra else (v.text or extra)
        parts = [(self.mic_lbl, f"Mic {v.mic}" if v.mic else ""), (self.call_lbl, f"Call {v.call}" if v.call else ""),
                 (self.stat_lbl, joined)]
        self.mic_lbl.configure(foreground=theme.MUTE_BAR_FG if v.mic_muted else "")
        layout = tuple(bool(t) for _, t in parts)
        if layout != self._status_layout:  # repack in order
            for lbl, _ in parts:
                lbl.pack_forget()
            first = True
            for lbl, text in parts:
                if text:
                    lbl.pack(side="left", padx=(0 if first else 12, 0))
                    first = False
            self._status_layout = layout
        for lbl, text in parts:
            if text and lbl.cget("text") != text:
                lbl.configure(text=text)
        self._set_bar_right(v.show_stop, v.show_stop and v.stop_enabled)
        if v.show_stop:
            self.stop_btn.configure(state="normal" if v.stop_enabled else "disabled")

    def _set_bar_right(self, show_stop: bool, show_bookmark: bool) -> None:
        """Stop (rightmost) and the Bookmark split button, repacked only when the layout changes."""
        layout = (show_stop, show_bookmark)
        if layout == self._bar_right:
            return
        for w in (self.stop_btn, self.bookmark_arrow, self.bookmark_btn):
            w.pack_forget()
        if show_stop:
            self.stop_btn.pack(side="right")
        if show_bookmark:
            self._fill_bookmark_menu()
            self.bookmark_arrow.pack(side="right", padx=(0, 8))
            self.bookmark_btn.pack(side="right")
        self._bar_right = layout

    def _popup_bookmark_menu(self) -> None:
        """The split button's arrow: the four types with their hotkeys, under the button."""
        self._fill_bookmark_menu()
        self.bookmark_menu.tk_popup(self.bookmark_arrow.winfo_rootx(),
                                    self.bookmark_arrow.winfo_rooty() + self.bookmark_arrow.winfo_height())

    def _fill_bookmark_menu(self) -> None:
        self.bookmark_menu.delete(0, "end")
        for item in logic.bookmark_items(self._hotkeys(), self._settings_kinds()):
            self.bookmark_menu.add_command(label=item.menu_text, command=lambda k=item.kind: self._bookmark(k))
