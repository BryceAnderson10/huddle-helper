"""The Storage panel (L32): disk use of the meetings folder, the auto-purge setting, and "Delete kept audio older than
N days" with a preview. Thin Tk over ``storage`` (the sizes, the previewed set, the wording) and ``confirm`` (the
delete wording); it is opened from the Meetings view's "Storage…" button.

Nothing here reads the disk: the numbers come from the Meetings view's last scan (``view.infos``), whose sizes the
library measures on a worker thread, and the delete runs through ``view.delete_ids`` (a worker thread; the same
``library.delete_audio`` as the Meetings list, so a meeting that is recording or being processed is refused, and
the Recycle Bin is the default). "Preview" freezes the list: the delete is given exactly the ids that were shown.
"""

from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable

from .. import config, library, shell, storage
from . import confirm, meetings_logic as ml, theme
from .dialogs import _place_over

REFRESH_MS = 1000


class StorageDialog:
    def __init__(self, parent: tk.Misc, app, *, infos: Callable[[], list[library.MeetingInfo]],
                 delete_ids: Callable[..., None], meetings_dir: Callable[[], object]):
        self.app, self._infos, self._delete_ids, self._meetings_dir = app, infos, delete_ids, meetings_dir
        self.plan: storage.PurgePlan | None = None  # the previewed set; None until Preview, cleared when N changes
        self._working = False
        self._job: str | None = None
        self.win = tk.Toplevel(parent)
        self.win.title("Storage")
        self.win.transient(parent.winfo_toplevel())
        self.win.resizable(False, False)
        scale = max(1.0, parent.winfo_fpixels("1i") / 96)
        self._wrap = int(520 * scale)
        small = (theme.FONT_FAMILY, theme.FONT_SIZE_SMALL)
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()

        # -- disk use
        ttk.Label(frm, text="Disk use of the meetings folder", font=(theme.FONT_FAMILY, 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.folder_lbl = ttk.Label(frm, text="", foreground=theme.MUTED, font=small, wraplength=self._wrap)
        self.folder_lbl.grid(row=1, column=0, columnspan=2, sticky="w")
        self.headline = ttk.Label(frm, text="", font=(theme.FONT_FAMILY, theme.FONT_SIZE, "bold"))
        self.headline.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.split = ttk.Label(frm, text="", wraplength=self._wrap)
        self.split.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="Largest by kept audio", foreground=theme.MUTED, font=small).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.largest = ttk.Treeview(frm, columns=("Meeting", "Date", "Audio"), show="headings", height=storage.LARGEST,
                                    selectmode="none")
        for col, w, anchor in (("Meeting", 280, "w"), ("Date", 110, "w"), ("Audio", 90, "e")):
            self.largest.heading(col, text=col, anchor=anchor)
            self.largest.column(col, width=int(w * scale), anchor=anchor, stretch=col == "Meeting")
        self.largest.grid(row=5, column=0, columnspan=2, sticky="we")

        # -- the auto-purge setting (the same setting as Settings → "Delete kept audio after")
        ttk.Separator(frm).grid(row=6, column=0, columnspan=2, sticky="we", pady=10)
        auto = ttk.Frame(frm)
        auto.grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(auto, text="Delete kept audio automatically after").pack(side="left")
        self.auto_days = tk.StringVar(value=str(app.settings.auto_purge_days))
        ttk.Spinbox(auto, from_=0, to=3650, textvariable=self.auto_days, width=6).pack(side="left", padx=6)
        ttk.Label(auto, text="days (0 = never)").pack(side="left")
        ttk.Button(auto, text="Save", command=self.save_auto).pack(side="left", padx=(10, 0))
        self.auto_note = ttk.Label(frm, text="", foreground=theme.MUTED, font=small, wraplength=self._wrap)
        self.auto_note.grid(row=8, column=0, columnspan=2, sticky="w")

        # -- delete now, with a preview
        ttk.Separator(frm).grid(row=9, column=0, columnspan=2, sticky="we", pady=10)
        now = ttk.Frame(frm)
        now.grid(row=10, column=0, columnspan=2, sticky="w")
        ttk.Label(now, text="Delete kept audio older than").pack(side="left")
        start = app.settings.auto_purge_days if app.settings.auto_purge_days > 0 else storage.DEFAULT_DAYS
        self.days = tk.StringVar(value=str(start))
        ttk.Spinbox(now, from_=1, to=3650, textvariable=self.days, width=6).pack(side="left", padx=6)
        ttk.Label(now, text="days").pack(side="left")
        self.preview_btn = ttk.Button(now, text="Preview…", command=self.preview)
        self.preview_btn.pack(side="left", padx=(10, 0))
        self.days.trace_add("write", lambda *_: self._invalidate())
        ttk.Label(frm, text="Only finished meetings are included, by the age of their final transcript, as for "
                            "automatic deletion. Transcripts and bookmarks stay.", foreground=theme.MUTED, font=small,
                  wraplength=self._wrap).grid(row=11, column=0, columnspan=2, sticky="w")

        self.pbox = ttk.Frame(frm)  # the preview: gridded only while there is one
        self.psummary = ttk.Label(self.pbox, text="", font=(theme.FONT_FAMILY, theme.FONT_SIZE, "bold"),
                                  wraplength=self._wrap)
        self.psummary.pack(anchor="w", pady=(0, 4))
        mid = ttk.Frame(self.pbox)
        mid.pack(fill="x")
        self.ptree = ttk.Treeview(mid, columns=("Meeting", "Finished", "Age", "Audio"), show="headings", height=6)
        for col, w, anchor in (("Meeting", 230, "w"), ("Finished", 100, "w"), ("Age", 70, "e"), ("Audio", 80, "e")):
            self.ptree.heading(col, text=col, anchor=anchor)
            self.ptree.column(col, width=int(w * scale), anchor=anchor, stretch=col == "Meeting")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.ptree.yview)
        self.ptree.configure(yscrollcommand=sb.set)
        self.ptree.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        self.pbody = ttk.Frame(self.pbox)
        self.pbody.pack(anchor="w", fill="x", pady=(6, 0))
        self.has_bin = shell.has_recycle_bin(meetings_dir())
        self.permanent = tk.BooleanVar(value=False)
        self.check = None
        if self.has_bin:
            self.check = ttk.Checkbutton(self.pbox, text="Delete permanently (skip the Recycle Bin)",
                                         variable=self.permanent, command=self._rebuild_preview)
            self.check.pack(anchor="w", pady=(8, 0))
        pbtns = ttk.Frame(self.pbox)
        pbtns.pack(anchor="e", pady=(8, 0))
        ttk.Button(pbtns, text="Cancel", command=self._invalidate).pack(side="left", padx=(0, 8))
        self.run_btn = ttk.Button(pbtns, text="Delete", command=self.run)
        self.run_btn.pack(side="left")

        self.result = tk.Label(frm, text="", anchor="w", justify="left", wraplength=self._wrap, font=small)
        self.result.grid(row=13, column=0, columnspan=2, sticky="we", pady=(8, 0))
        close = ttk.Frame(frm)
        close.grid(row=14, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(close, text="Close", command=self.close).pack()
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._frm = frm
        self.refresh()
        _place_over(self.win, parent)
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- keeping the numbers current ---------------------------------------------------------------------------
    def _tick(self) -> None:
        self._job = None
        try:
            self.refresh()
            self._job = self.win.after(REFRESH_MS, self._tick)
        except tk.TclError:
            pass  # closed

    def close(self) -> None:
        if self._job is not None:
            try:
                self.win.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        self.win.destroy()

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def refresh(self) -> storage.Usage:
        """Redraw the disk use from the Meetings view's last scan (no disk access; the preview is not touched)."""
        infos = self._infos()
        u = storage.usage(infos)
        self.folder_lbl.configure(text=str(self._meetings_dir()))
        head, split = storage.usage_lines(u)
        self.headline.configure(text=head)
        self.split.configure(text=split)
        rows = [(i.title, ml.date_text(i.started_at), confirm.format_size(i.audio_bytes)) for i in u.largest]
        if rows != getattr(self, "_largest_shown", None):
            self._largest_shown = rows
            self.largest.delete(*self.largest.get_children())
            for r in rows:
                self.largest.insert("", "end", values=r)
        days = self.app.settings.auto_purge_days
        self.auto_note.configure(text=storage.setting_text(days))
        return u

    def save_auto(self) -> str:
        """Save the automatic deletion setting (the same ``auto_purge_days`` as Settings). Returns the message shown."""
        try:
            days = int(self.auto_days.get())
            if days < 0:
                raise ValueError
        except ValueError:
            return self._say("Days must be a whole number, 0 or more.", "error")
        s = config.Settings.from_dict(self.app.settings.to_dict())
        s.auto_purge_days = days
        self.app.update_settings(s)
        self.refresh()
        return self._say("Saved. " + storage.setting_text(days), "info")

    # -- preview and delete ---------------------------------------------------------------------------------------
    def _days_value(self) -> int | None:
        try:
            days = int(self.days.get())
        except ValueError:
            return None
        return days if days >= 1 else None

    def preview(self) -> storage.PurgePlan | None:
        """Work out the set to delete now and show it. This set is what ``run`` deletes."""
        if self._working:
            return None
        days = self._days_value()
        if days is None:
            self._say("Days must be a whole number, 1 or more.", "error")
            return None
        self.plan = storage.plan_purge(self._infos(), days, self.app.live_state(), now=time.time())
        self._show_preview()
        self._say("", "info")
        return self.plan

    def _invalidate(self) -> None:
        """The number of days changed, or the preview was cancelled: the old list no longer applies."""
        if self._working:
            return
        self.plan = None
        self.pbox.grid_remove()

    def _show_preview(self) -> None:
        plan = self.plan
        self.psummary.configure(text=storage.purge_summary(plan))
        self.ptree.delete(*self.ptree.get_children())
        for it in plan.items:
            self.ptree.insert("", "end", values=(it.title, datetime.fromtimestamp(it.done_at).strftime("%Y-%m-%d"), f"{int(it.age_days)} d",
                                                 confirm.format_size(it.audio_bytes) if it.audio_bytes is not None else "…"))
        self.pbox.grid(row=12, column=0, columnspan=2, sticky="we", pady=(8, 0))
        self._rebuild_preview()

    def _rebuild_preview(self) -> None:
        plan = self.plan
        if plan is None:
            return
        for child in self.pbody.winfo_children():
            child.destroy()
        if plan.empty:
            self.run_btn.configure(text="Delete", state="disabled")
            if self.check is not None:
                self.check.configure(state="disabled")
            return
        view = confirm.delete_view(storage.confirm_items(plan), audio_only=True, has_bin=self.has_bin,
                                   permanent=self.permanent.get())
        for text in view.details:
            ttk.Label(self.pbody, text=text, wraplength=self._wrap, justify="left").pack(anchor="w", pady=(0, 3))
        for text in view.warnings:
            tk.Label(self.pbody, text=text, wraplength=self._wrap, justify="left", anchor="w", bg=theme.MUTE_BAR_BG,
                     fg=theme.MUTE_BAR_FG, padx=8, pady=4).pack(anchor="w", fill="x", pady=(2, 3))
        if not plan.complete:
            tk.Label(self.pbody, text="Some sizes are still being measured, so the total may be higher. "
                                      "Press Preview again in a moment to delete.", wraplength=self._wrap,
                     justify="left", anchor="w", bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG, padx=8, pady=4
                     ).pack(anchor="w", fill="x", pady=(2, 3))
        self.run_btn.configure(text=view.confirm, state="normal" if plan.complete and not self._working else "disabled")
        if self.check is not None:
            self.check.configure(state="normal")

    def run(self) -> None:
        """Delete exactly the previewed set (the Recycle Bin unless "permanently" is ticked)."""
        plan = self.plan
        if plan is None or plan.empty or not plan.complete or self._working:
            return
        permanent = bool(self.permanent.get()) and self.has_bin
        ids = plan.ids
        self._working = True
        self.run_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self._say("Deleting…", "info")
        self._delete_ids(ids, permanent, lambda result, p=plan: self._done(p, result))

    def _done(self, plan: storage.PurgePlan, result: library.DeleteResult) -> None:
        self._working = False
        try:
            self.preview_btn.configure(state="normal")
        except tk.TclError:
            return  # the dialog was closed meanwhile
        text, level = ml.delete_message(result, audio_only=True)
        if result.skipped:
            text += f" {len(result.skipped)} had no audio left."
        self.plan = None
        self.pbox.grid_remove()
        self._say(text, level)

    def _say(self, text: str, level: str = "info") -> str:
        bg, fg = (theme.FAILED_BG, theme.FAILED_FG) if level == "error" else (theme.NOTICE_BG, theme.NOTICE_FG)
        if text:
            self.result.configure(text=text, bg=bg, fg=fg, padx=8, pady=4)
        else:
            self.result.configure(text="", bg=self.win.cget("bg"), padx=0, pady=0)
        return text
