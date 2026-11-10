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
"""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from .. import marks as marks_mod, meeting as mt
from ..session import MeetingSession
from ..transcript_model import LineRow, MarkRow, Reset
from . import logic, theme
from .dialogs import ExportDialog, MarkEditor
from .meetings_view import MeetingsView
from .transcript_view import TranscriptView

log = logging.getLogger(__name__)

FAST_MS, FAST_MORE_MS, STATUS_MS, SLOW_MS = 250, 20, 500, 1000
FEEDBACK_S = 3.0  # how long "Decision added" stays in the status bar


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
        ttk.Separator(self.tpage, orient="horizontal").pack(fill="x")

        # amber mute bar (packed only while muted)
        self.mute_bar = tk.Frame(self.tpage, bg=theme.MUTE_BAR_BG)
        tk.Label(self.mute_bar, text="MIC MUTED", bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG,
                 font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL, "bold")).pack(side="left", padx=(10, 8), pady=4)
        self.mute_bar_text = tk.Label(self.mute_bar, text="", bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG,
                                      font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL), anchor="w")
        self.mute_bar_text.pack(side="left", pady=4)
        ttk.Button(self.mute_bar, text="Unmute", command=self._toggle_mute).pack(side="right", padx=10, pady=3)
        self._mute_bar_shown = False

        # notice bar (packed only when there is a notice)
        self.notice = tk.Label(self.tpage, text="", anchor="w", justify="left", font=base,
                               padx=int(10 * scale), pady=int(4 * scale), wraplength=int(740 * scale))
        self._notice_shown = False

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
        self.view.bookmark_items = lambda: logic.bookmark_items(self._hotkeys(), self._custom_kinds())

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
        self.view.clear()
        self.session = MeetingSession(folder)
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
        return dict(getattr(self.app.settings, "hotkeys", None) or {})

    def _custom_kinds(self) -> dict[str, str]:
        return {k: v for k, v in (self.session.model.mark_kinds if self.session else {}).items()
                if k not in marks_mod.KIND_NAMES}

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
                   on_delete=lambda: self._delete_mark(row))

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
        self._set_notice(logic.notice_view(mid, model, state))
        self._set_status(logic.status_view(mid, model, state))

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
            if self.mute_bar_text.cget("text") != bar.text:
                self.mute_bar_text.configure(text=bar.text)
            if not self._mute_bar_shown:
                self.mute_bar.pack(fill="x", before=self.notice if self._notice_shown else self.view)
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
        bg, fg = (theme.FAILED_BG, theme.FAILED_FG) if n.level == "error" else (theme.NOTICE_BG, theme.NOTICE_FG)
        if self.notice.cget("text") != n.text or self.notice.cget("bg") != bg:
            self.notice.configure(text=n.text, bg=bg, fg=fg)
        if not self._notice_shown:
            self.notice.pack(fill="x", before=self.view)
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
        for item in logic.bookmark_items(self._hotkeys(), self._custom_kinds()):
            self.bookmark_menu.add_command(label=item.menu_text, command=lambda k=item.kind: self._bookmark(k))
