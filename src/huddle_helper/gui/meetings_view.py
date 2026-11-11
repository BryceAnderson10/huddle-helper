"""The Meetings view (L29-L31): every meeting in a list, with the actions that apply to it.

Thin Tk over ``meetings_logic`` (rows, which buttons are on, messages), ``confirm`` (the delete
wording) and ``library`` (scan, delete). It shares the main window with the Transcript view and is
swapped in place (plan 4.5): ``MainWindow`` packs one or the other.

Threads: the folder scan and every delete run on short-lived worker threads and hand their result back
through ``post`` (``TrayApp.calls`` in the app), so a large meetings folder or a slow delete never
blocks Tk. The list is redrawn from the last scan plus ``AppController.live_state()`` on each ``tick``
(the window's 500 ms status timer), so a final pass's percentage moves without any file reading.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from .. import library, meeting as mt, shell
from ..library import MeetingInfo
from . import meetings_logic as ml, theme
from .dialogs import DeleteDialog, ExportDialog, browse_for_meeting
from .storage_dialog import StorageDialog

log = logging.getLogger(__name__)

SCAN_EVERY_S = 2.0
MESSAGE_S = 8.0  # how long "Moved 2 meetings to the Recycle Bin" stays
WIDTHS = {"Date": 96, "Title": 210, "Length": 62, "Status": 190, "Speakers": 66, "Marks": 50, "Audio": 70}
ANCHORS = {"Length": "e", "Speakers": "e", "Marks": "e", "Audio": "e"}
ROW_COLORS = {library.FAILED: theme.FAILED_FG, library.RETRY_WAITING: theme.FAILED_FG,
              library.RECORDING: theme.RECORDING, library.PROCESSING: theme.MUTE_BAR_FG,
              library.QUEUED: theme.MUTE_BAR_FG, library.RECOVERED: theme.TEXT,
              library.UNFINISHED: theme.MUTED, library.COMPLETE: theme.TEXT}


class MeetingsView(ttk.Frame):
    def __init__(self, parent: tk.Misc, app, *, post: Callable[[Callable[[], None]], None],
                 on_open: Callable[[mt.MeetingFolder], None], on_back: Callable[[], None],
                 has_session: Callable[[], bool], on_deleted: Callable[[list[str]], None]):
        super().__init__(parent)
        self.app, self.post = app, post
        self._on_open, self._on_back, self._has_session, self._on_deleted = on_open, on_back, has_session, on_deleted
        self.cache = library.ScanCache()
        self.infos: list[MeetingInfo] = []
        self._by_id: dict[str, MeetingInfo] = {}
        self._dir: Path | None = None
        self._scanning = False
        self._last_scan = 0.0
        self._scanned_once = False
        self._working = False  # a delete is running
        self._message: tuple[str, str, float] = ("", "", 0.0)
        self._shown: dict[str, tuple] = {}
        self._busy_before: frozenset[str] = frozenset()
        self._storage: StorageDialog | None = None
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        style = ttk.Style(self)
        style.configure("Meetings.Treeview", rowheight=int(24 * scale))
        base = (theme.FONT_FAMILY, theme.FONT_SIZE_SMALL)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Meetings", font=(theme.FONT_FAMILY, 11, "bold")).pack(side="left")
        self.summary = ttk.Label(top, text="", font=base, foreground=theme.MUTED)
        self.summary.pack(side="left", padx=(12, 0))
        self.back_btn = ttk.Button(top, text="Transcript", command=self._on_back)
        self.back_btn.pack(side="right")
        ttk.Button(top, text="Storage…", command=self.open_storage).pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Browse…", command=self._browse).pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Open meetings folder", command=self._open_root).pack(side="right", padx=(0, 6))

        filt = ttk.Frame(self, padding=(10, 0, 10, 6))
        filt.pack(fill="x")
        ttk.Label(filt, text="Show", font=base).pack(side="left")
        self.filter_var = tk.StringVar(value="All")
        self.filter_box = ttk.Combobox(filt, textvariable=self.filter_var, values=list(library.FILTERS),
                                       state="readonly", width=18)
        self.filter_box.pack(side="left", padx=(6, 14))
        ttk.Label(filt, text="Search", font=base).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_box = ttk.Entry(filt, textvariable=self.search_var, width=28)
        self.search_box.pack(side="left", padx=(6, 0))
        ttk.Button(filt, text="Clear", width=6, command=self._clear_filters).pack(side="left", padx=(6, 0))
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self._render())
        self.search_var.trace_add("write", lambda *a: self._render())

        # The bottom parts are packed before the list so they keep their place when the window is small.
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(side="bottom", fill="x")
        self._sep = ttk.Separator(self, orient="horizontal")
        self._sep.pack(side="bottom", fill="x", pady=(6, 0))
        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(mid, columns=ml.COLUMNS, show="headings", selectmode="extended",
                                 style="Meetings.Treeview")
        for col in ml.COLUMNS:
            self.tree.heading(col, text=col, anchor=ANCHORS.get(col, "w"))
            self.tree.column(col, width=int(WIDTHS[col] * scale), anchor=ANCHORS.get(col, "w"),
                             stretch=col == "Title", minwidth=int(40 * scale))
        for kind, color in ROW_COLORS.items():
            self.tree.tag_configure(kind, foreground=color)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.empty = ttk.Label(mid, text="", foreground=theme.MUTED, anchor="center")

        self.detail = tk.Label(self, text="", anchor="w", justify="left", font=base, padx=int(10 * scale),
                               pady=int(5 * scale), wraplength=int(740 * scale))
        self._detail_shown = False

        self.buttons = {
            "open": ttk.Button(bar, text="Open", command=self.open_selected),
            "export": ttk.Button(bar, text="Export…", command=self.export_selected),
            "explorer": ttk.Button(bar, text="Show in Explorer", command=self.show_in_explorer),
            "retry": ttk.Button(bar, text="Retry final pass", command=self.retry_selected),
            "delete_audio": ttk.Button(bar, text="Delete audio", command=lambda: self.delete(audio_only=True)),
            "delete_meeting": ttk.Button(bar, text="Delete meeting", command=lambda: self.delete(audio_only=False)),
        }
        for key in ("open", "export", "explorer", "retry"):
            self.buttons[key].pack(side="left", padx=(0, 6))
        self.buttons["delete_meeting"].pack(side="right")
        self.buttons["delete_audio"].pack(side="right", padx=(0, 6))

        self.menu = tk.Menu(self.tree, tearoff=False)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        self.tree.bind("<Double-Button-1>", self._double_click)
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Delete>", lambda e: self.delete(audio_only=False))
        self.tree.bind("<Shift-Delete>", lambda e: self.delete(audio_only=False, permanent=True))
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Button-3>", self._popup)

    # -- showing -------------------------------------------------------------------------------
    def activate(self) -> None:
        """The view was just shown: scan now, not at the next 2 s mark."""
        self._last_scan = 0.0
        self.tick()

    def tick(self) -> None:
        """Every 500 ms while visible: start a scan when due, redraw from the last one."""
        self._keep_scanning()
        self.back_btn.configure(state="normal" if self._has_session() else "disabled")
        self._render()

    def current_infos(self) -> list[MeetingInfo]:
        """The last scan of the meetings folder as it is now, for the Storage panel: it keeps scanning (and follows a
        folder change in Settings) while the panel is open, even when this list is not shown and so not ticking."""
        self._keep_scanning()
        return self.infos

    def _keep_scanning(self) -> None:
        meetings_dir = self.app.settings.resolved_meetings_dir()
        if meetings_dir != self._dir:  # another folder (Settings): start over
            self._dir = meetings_dir
            self.cache = library.ScanCache()
            self.infos, self._by_id, self._scanned_once = [], {}, False
            self._last_scan = 0.0
        if not self._scanning and time.monotonic() - self._last_scan >= SCAN_EVERY_S:
            self._kick_scan()

    def _kick_scan(self) -> None:
        self._scanning = True
        meetings_dir, cache = self._dir, self.cache

        def work() -> None:
            try:
                infos = library.scan(meetings_dir, cache)
            except Exception:  # noqa: BLE001 - keep the last list
                log.exception("Scanning %s failed", meetings_dir)
                infos = None
            self.post(lambda: self._scanned(meetings_dir, infos))

        threading.Thread(target=work, name="library-scan", daemon=True).start()

    def _scanned(self, meetings_dir: Path, infos: list[MeetingInfo] | None) -> None:
        self._scanning = False
        self._last_scan = time.monotonic()
        if infos is None or meetings_dir != self._dir:
            return
        self.infos = infos
        self._by_id = {i.id: i for i in infos}
        self._scanned_once = True
        self._render()

    # -- drawing ---------------------------------------------------------------------------------
    def _render(self) -> None:
        live = self.app.live_state()
        busy = frozenset(live.queued | live.waiting | {m for m in (live.recording, live.finalizing) if m})
        if busy != self._busy_before:  # a meeting started or stopped being worked on: its meta.json changed
            self._busy_before = busy
            self._last_scan = 0.0  # so rescan now, not at the next 2 s mark (the old row would be mislabelled)
        rows = ml.meeting_rows(self.infos, live, status_filter=self.filter_var.get(), search=self.search_var.get())
        ids = [r.id for r in rows]
        have = list(self.tree.get_children())
        keep = set(ids)
        for iid in have:
            if iid not in keep:
                self.tree.delete(iid)
                self._shown.pop(iid, None)
        existing = set(self.tree.get_children())
        for r in rows:
            tags = (r.kind,)
            if r.id not in existing:
                self.tree.insert("", "end", iid=r.id, values=r.values, tags=tags)
            elif self._shown.get(r.id) != (r.values, tags):
                self.tree.item(r.id, values=r.values, tags=tags)
            self._shown[r.id] = (r.values, tags)
        if list(self.tree.get_children()) != ids:
            for i, iid in enumerate(ids):
                self.tree.move(iid, "", i)
        text = ml.summary_text(self.infos, shown=len(rows))
        if self.summary.cget("text") != text:
            self.summary.configure(text=text)
        self._set_empty(rows)
        self._update_buttons(live)

    def _set_empty(self, rows) -> None:
        if rows:
            self.empty.place_forget()
            return
        if not self._scanned_once:
            msg = "Looking for meetings…"
        elif self.infos:
            msg = "No meeting matches the filter."
        else:
            msg = f"No meetings in {self._dir}"
        if self.empty.cget("text") != msg:
            self.empty.configure(text=msg)
        self.empty.place(relx=0.5, rely=0.45, anchor="center")

    def selected(self) -> list[MeetingInfo]:
        return [self._by_id[i] for i in self.tree.selection() if i in self._by_id]

    def _update_buttons(self, live: library.LiveState | None = None) -> None:
        live = live or self.app.live_state()
        sel = self.selected()
        act = ml.actions_for(sel, live)
        for key, button in self.buttons.items():
            on = getattr(act, key) and not self._working
            state = "normal" if on else "disabled"
            if str(button.cget("state")) != state:
                button.configure(state=state)
        text, level = self._message[0], self._message[1]
        if not text or time.monotonic() > self._message[2]:
            text, level = ml.detail_text(sel, live)
            if not text and act.why:
                text, level = act.why, "info"
        self._set_detail(text, level)

    def _set_detail(self, text: str, level: str) -> None:
        if not text:
            if self._detail_shown:
                self.detail.pack_forget()
                self._detail_shown = False
            return
        bg, fg = (theme.FAILED_BG, theme.FAILED_FG) if level == "error" else (theme.NOTICE_BG, theme.NOTICE_FG)
        if self.detail.cget("text") != text or self.detail.cget("bg") != bg:
            self.detail.configure(text=text, bg=bg, fg=fg)
        if not self._detail_shown:
            self.detail.pack(side="bottom", fill="x", after=self._sep)
            self._detail_shown = True

    def say(self, text: str, level: str = "info") -> None:
        self._message = (text, level, time.monotonic() + MESSAGE_S)
        self._update_buttons()

    # -- selection and menu ---------------------------------------------------------------------------
    def _select_all(self, _event=None) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _clear_filters(self) -> None:
        self.filter_var.set("All")
        self.search_var.set("")

    def _double_click(self, event) -> None:
        if self.tree.identify_row(event.y):
            self.open_selected()

    def _popup(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        act = ml.actions_for(self.selected(), self.app.live_state())
        self.menu.delete(0, "end")
        for label, key, cmd in (
                ("Open", "open", self.open_selected), ("Export…", "export", self.export_selected),
                ("Show in Explorer", "explorer", self.show_in_explorer),
                ("Retry final pass", "retry", self.retry_selected),
                ("Delete audio", "delete_audio", lambda: self.delete(audio_only=True)),
                ("Delete meeting", "delete_meeting", lambda: self.delete(audio_only=False))):
            self.menu.add_command(label=label, command=cmd, state="normal" if getattr(act, key) else "disabled")
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- actions ---------------------------------------------------------------------------------------
    def _one(self) -> MeetingInfo | None:
        sel = self.selected()
        return sel[0] if len(sel) == 1 else None

    def open_selected(self) -> None:
        info = self._one()
        if info is not None:
            self._on_open(mt.MeetingFolder(info.path))

    def export_selected(self) -> None:
        info = self._one()
        if info is None:
            return
        try:
            model = ml.load_model(mt.MeetingFolder(info.path))
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not read %s for export", info.id)
            self.say(f"Could not read that meeting: {exc}", "error")
            return
        ExportDialog(self, model)

    def show_in_explorer(self) -> None:
        info = self._one()
        if info is not None:
            os.startfile(info.path)  # noqa: S606 - opens the meeting's folder in Explorer

    def _open_root(self) -> None:
        path = self.app.settings.resolved_meetings_dir()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606

    def _browse(self) -> None:
        folder = browse_for_meeting(self)
        if folder is not None:
            self._on_open(folder)

    def retry_selected(self) -> None:
        failed = [i for i in self.selected() if library.status_of(i, self.app.live_state()).kind == library.FAILED]
        outcomes = [self.app.retry_final(i.id) for i in failed]
        text, level = ml.retry_message(outcomes)
        self._last_scan = 0.0  # meta.json changed: rescan soon
        self.say(text, level)
        self._render()

    def delete(self, *, audio_only: bool, permanent: bool = False) -> None:
        """Confirm, then delete on a worker thread. ``permanent`` pre-ticks the check box (Shift+Delete)."""
        if self._working:
            return
        sel = self.selected()
        act = ml.actions_for(sel, self.app.live_state())
        if not (act.delete_audio if audio_only else act.delete_meeting):
            return
        items = ml.confirm_items(sel, audio_only=audio_only)
        targets = [i.id for i in sel if i.has_audio or not audio_only]
        has_bin = shell.has_recycle_bin(self.app.settings.resolved_meetings_dir())
        DeleteDialog(self, items, audio_only=audio_only, has_bin=has_bin, permanent=permanent,
                     on_confirm=lambda perm: self._run_delete(targets, audio_only, perm))

    # -- Storage panel (L32) ---------------------------------------------------------------------------------
    def open_storage(self) -> StorageDialog:
        """The Storage panel (one at a time): disk use, the auto-delete setting, delete kept audio older than N days."""
        dlg = self._storage
        if dlg is not None and dlg.alive():
            dlg.win.lift()
            return dlg
        self._storage = StorageDialog(self, self.app, infos=self.current_infos, delete_ids=self.delete_ids,
                                      meetings_dir=self.app.settings.resolved_meetings_dir)
        return self._storage

    def delete_ids(self, ids: list[str], permanent: bool, done: Callable[[library.DeleteResult], None]) -> None:
        """Delete the kept audio of exactly these meetings (already confirmed by the caller) on a worker thread;
        ``done(result)`` runs on the Tk thread afterwards."""
        self._run_delete(list(ids), True, permanent, done)

    def _run_delete(self, ids: list[str], audio_only: bool, permanent: bool, done=None) -> None:
        self._working = True
        self.say("Deleting…")

        def work() -> None:
            try:
                result = self.app.delete_meetings(ids, audio_only=audio_only, permanent=permanent)
            except Exception as exc:  # noqa: BLE001
                log.exception("Delete failed")
                result = library.DeleteResult(failed=[(i, str(exc)) for i in ids])
            self.post(lambda: self._deleted(result, audio_only, done))

        threading.Thread(target=work, name="delete-meetings", daemon=True).start()

    def _deleted(self, result: library.DeleteResult, audio_only: bool, done=None) -> None:
        self._working = False
        text, level = ml.delete_message(result, audio_only=audio_only)
        for mid in result.done:
            if not audio_only:
                self.cache.forget(str(self._dir / mid))
        self._last_scan = 0.0
        self.say(text, level)
        if result.done and not audio_only:
            self._on_deleted(list(result.done))
        self.tick()
        if done is not None:
            done(result)
