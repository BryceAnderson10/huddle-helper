"""Small dialogs of the Transcript window: Export, Open meeting and the bookmark editor. Thin Tk over ``logic``."""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .. import export as export_mod, meeting as mt
from .. import marks as marks_mod
from ..transcript_model import MarkRow, TranscriptModel
from . import logic, theme

log = logging.getLogger(__name__)


def _place_over(win: tk.Toplevel, parent: tk.Misc) -> None:
    win.update_idletasks()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_width()) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height() - win.winfo_height()) // 3)
    win.geometry(f"+{x}+{y}")


class ExportDialog:
    """Format, times, join; then Copy to clipboard or Save as. Exports what the window shows (so the
    live file during a meeting, marked provisional, and the final file after)."""

    def __init__(self, parent: tk.Misc, model: TranscriptModel):
        self.model = model
        self.win = tk.Toplevel(parent)
        self.win.title("Export transcript")
        self.win.transient(parent.winfo_toplevel())
        self.win.resizable(False, False)
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        self.fmt = tk.StringVar(value="md")
        self.times = tk.StringVar(value="elapsed")
        self.join = tk.BooleanVar(value=True)

        ttk.Label(frm, text="Format").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 14))
        f = ttk.Frame(frm)
        f.grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(f, text="Markdown (.md)", variable=self.fmt, value="md").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(f, text="Text (.txt)", variable=self.fmt, value="txt").pack(side="left")
        ttk.Label(frm, text="Times").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 14))
        f = ttk.Frame(frm)
        f.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(f, text="Since start", variable=self.times, value="elapsed").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(f, text="Clock", variable=self.times, value="clock").pack(side="left")
        ttk.Checkbutton(frm, text="Join consecutive lines of the same speaker", variable=self.join).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=3)
        if model.provisional:
            ttk.Label(frm, text="The final transcript is not ready, so this export is marked provisional.",
                      wraplength=int(360 * max(1.0, parent.winfo_fpixels("1i") / 96)),
                      foreground=theme.NOTICE_FG).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.status = ttk.Label(frm, text="", foreground=theme.MUTED)
        self.status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Copy to clipboard", command=self.copy).pack(side="left", padx=4)
        ttk.Button(btns, text="Save as…", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side="left", padx=4)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _place_over(self.win, parent)

    def _text(self, clipboard: bool) -> str:
        return logic.export_text(self.model, self.fmt.get(), self.times.get(), self.join.get(),
                                 clipboard=clipboard)

    def copy(self) -> None:
        try:
            text = self._text(True)
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.status.configure(text=f"Copied {len(text):,} characters to the clipboard.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Export copy failed")
            self.status.configure(text=f"Could not copy: {exc}")

    def save(self) -> None:
        fmt = self.fmt.get()
        folder = self.model.folder.path.parent if self.model.folder is not None else Path.home()
        path = filedialog.asksaveasfilename(
            parent=self.win, title="Save transcript", defaultextension=f".{fmt}",
            initialdir=str(Path.home() / "Documents") if (Path.home() / "Documents").exists() else str(folder),
            initialfile=export_mod.export_filename(self.model, fmt),
            filetypes=[("Markdown", "*.md")] if fmt == "md" else [("Text", "*.txt")])
        if not path:
            return
        try:
            export_mod.write_export(Path(path), self._text(False))
            self.status.configure(text=f"Saved to {path}")
        except OSError as exc:
            messagebox.showerror("Huddle Helper", f"Could not save the file: {exc}", parent=self.win)


class MarkEditor:
    """Edit a bookmark: label, type, or delete it. ``on_save(changes)`` gets the keyword arguments for
    ``AppController.edit_mark`` (empty when nothing changed), ``on_delete()`` the delete request."""

    def __init__(self, parent: tk.Misc, model: TranscriptModel, row: MarkRow,
                 on_save: Callable[[dict], None], on_delete: Callable[[], None]):
        self.row = row
        self._on_save, self._on_delete = on_save, on_delete
        self.win = tk.Toplevel(parent)
        self.win.title("Edit bookmark")
        self.win.transient(parent.winfo_toplevel())
        self.win.resizable(False, False)
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        ttk.Label(frm, text=f"At {logic.line_time(model, row.at_ms)}", foreground=theme.MUTED).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frm, text="Type").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 12))
        kinds = [*marks_mod.KINDS, *[k for k in model.mark_kinds if k not in marks_mod.KINDS]]
        self._names = {model.kind_name(k): k for k in kinds}
        self.kind = tk.StringVar(value=model.kind_name(row.kind))
        ttk.Combobox(frm, textvariable=self.kind, state="readonly", width=24,
                     values=list(self._names)).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="Label").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 12))
        self.label = tk.StringVar(value=row.label)
        self.entry = ttk.Entry(frm, textvariable=self.label, width=40)
        self.entry.grid(row=2, column=1, sticky="w", pady=3)
        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="we", pady=(12, 0))
        ttk.Button(btns, text="Delete bookmark", command=self._delete).pack(side="left")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)
        self.win.bind("<Return>", lambda e: self._save())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _place_over(self.win, parent)
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def _save(self) -> None:
        changes = logic.mark_changes(self.row, self.label.get(), self._names.get(self.kind.get(), self.row.kind))
        self.win.destroy()
        if changes:
            self._on_save(changes)

    def _delete(self) -> None:
        self.win.destroy()
        self._on_delete()


class MeetingPicker:
    """Simple 'Open meeting' list (replaced by the Meetings view in a later work package)."""

    def __init__(self, parent: tk.Misc, meetings_dir: Path, on_open: Callable[[mt.MeetingFolder], None]):
        self.on_open = on_open
        self.entries = logic.list_meetings(meetings_dir)
        self.win = tk.Toplevel(parent)
        self.win.title("Open meeting")
        self.win.transient(parent.winfo_toplevel())
        frm = ttk.Frame(self.win, padding=12)
        frm.grid(sticky="nsew")
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)
        ttk.Label(frm, text=f"Meetings in {meetings_dir}" if self.entries else
                  f"No meetings in {meetings_dir}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.box = tk.Listbox(frm, width=78, height=14, activestyle="dotbox", exportselection=False,
                              font=(theme.FONT_FAMILY, theme.FONT_SIZE))
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.box.yview)
        self.box.configure(yscrollcommand=sb.set)
        self.box.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        for e in self.entries:
            self.box.insert("end", e.label)
        if self.entries:
            self.box.selection_set(0)
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Browse for a folder…", command=self._browse).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="Open", command=self._open).pack(side="left", padx=4)
        self.box.bind("<Double-Button-1>", lambda e: self._open())
        self.box.bind("<Return>", lambda e: self._open())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _place_over(self.win, parent)
        self.box.focus_set()

    def _open(self) -> None:
        sel = self.box.curselection()
        if not sel:
            return
        folder = mt.MeetingFolder(self.entries[sel[0]].path)
        self.win.destroy()
        self.on_open(folder)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.win, title="Choose a meeting folder")
        if not chosen:
            return
        folder = mt.MeetingFolder(Path(chosen))
        if not folder.meta_path.exists():
            messagebox.showerror("Huddle Helper", "That folder has no meta.json, so it is not a meeting.",
                                 parent=self.win)
            return
        self.win.destroy()
        self.on_open(folder)
