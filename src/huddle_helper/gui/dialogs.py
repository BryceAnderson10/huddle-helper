"""Small dialogs of the Transcript window: Export, the bookmark editor and the delete confirmation. Thin Tk over ``logic``."""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .. import export as export_mod, meeting as mt
from .. import marks as marks_mod
from ..transcript_model import MarkRow, TranscriptModel
from . import confirm, logic, theme

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
                 on_save: Callable[[dict], None], on_delete: Callable[[], None],
                 custom: dict[str, str] | None = None):
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
        # the four built-in types, then the user-defined ones (``custom``: this meeting's own and Settings' now)
        names = {**(custom or {}), **{k: v for k, v in model.mark_kinds.items() if k not in marks_mod.KIND_NAMES}}
        shown = {**marks_mod.KIND_NAMES, **names}
        self._names = {shown[k]: k for k in [*marks_mod.KINDS, *names]}
        self.kind = tk.StringVar(value=shown.get(row.kind, model.kind_name(row.kind)))
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


class DeleteDialog:
    """The delete confirmation. All wording comes from ``confirm.delete_view``; this only lays it out.
    ``on_confirm(permanent)`` runs after the user presses the red button; Cancel and Esc do nothing.
    With a Recycle Bin there is a check box for permanent delete (ticked when the user pressed Shift+Delete)."""

    def __init__(self, parent: tk.Misc, items: list[confirm.Item], *, audio_only: bool, has_bin: bool,
                 permanent: bool, on_confirm: Callable[[bool], None]):
        self.items, self.audio_only, self.has_bin = items, audio_only, has_bin
        self._on_confirm = on_confirm
        self.win = tk.Toplevel(parent)
        self.win.transient(parent.winfo_toplevel())
        self.win.resizable(False, False)
        scale = max(1.0, parent.winfo_fpixels("1i") / 96)
        self._wrap = int(440 * scale)
        self.view = confirm.delete_view(items, audio_only=audio_only, has_bin=has_bin, permanent=permanent)
        self.permanent = tk.BooleanVar(value=self.view.permanent and has_bin)
        frm = ttk.Frame(self.win, padding=16)
        frm.grid()
        self.heading = ttk.Label(frm, wraplength=self._wrap, justify="left",
                                 font=(theme.FONT_FAMILY, theme.FONT_SIZE, "bold"))
        self.heading.grid(row=0, column=0, sticky="w")
        self.body = ttk.Frame(frm)
        self.body.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.check = None
        if has_bin:
            self.check = ttk.Checkbutton(frm, text="Delete permanently (skip the Recycle Bin)",
                                         variable=self.permanent, command=self._rebuild)
            self.check.grid(row=2, column=0, sticky="w", pady=(10, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, sticky="e", pady=(14, 0))
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.win.destroy)
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.ok_btn = ttk.Button(btns, command=self._ok)
        self.ok_btn.pack(side="left")
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Return>", self._return)
        self._rebuild()
        _place_over(self.win, parent)
        self.win.grab_set()
        (self.cancel_btn if self.view.cancel_default else self.ok_btn).focus_set()

    def _rebuild(self) -> None:
        self.view = confirm.delete_view(self.items, audio_only=self.audio_only, has_bin=self.has_bin,
                                        permanent=self.permanent.get())
        v = self.view
        self.win.title(v.title)
        self.heading.configure(text=v.heading)
        for child in self.body.winfo_children():
            child.destroy()
        for text in v.details:
            ttk.Label(self.body, text=text, wraplength=self._wrap, justify="left").pack(anchor="w", pady=(0, 4))
        for text in v.warnings:
            tk.Label(self.body, text=text, wraplength=self._wrap, justify="left", anchor="w",
                     bg=theme.MUTE_BAR_BG, fg=theme.MUTE_BAR_FG, padx=8, pady=5,
                     font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL)).pack(anchor="w", fill="x", pady=(2, 4))
        self.ok_btn.configure(text=v.confirm)

    def _return(self, _event) -> str:
        """Enter presses the focused button; with focus elsewhere, the safe one when there is a warning."""
        focused = self.win.focus_get()
        if focused is self.ok_btn or (focused is not self.cancel_btn and not self.view.cancel_default):
            self._ok()
        else:
            self.win.destroy()
        return "break"

    def _ok(self) -> None:
        permanent = self.view.permanent
        self.win.destroy()
        self._on_confirm(permanent)


def browse_for_meeting(parent: tk.Misc) -> mt.MeetingFolder | None:
    """Ask for any meeting folder (one outside the meetings folder, such as a copy)."""
    chosen = filedialog.askdirectory(parent=parent, title="Choose a meeting folder")
    if not chosen:
        return None
    folder = mt.MeetingFolder(Path(chosen))
    if not folder.meta_path.exists():
        messagebox.showerror("Huddle Helper", "That folder has no meta.json, so it is not a meeting.", parent=parent)
        return None
    return folder


class NamePicker:
    """The name picker: an entry, and the names already used in this meeting to click. Enter (or a
    double click on a name) accepts, Esc cancels. ``on_done(name)`` gets the trimmed, non-empty name."""

    def __init__(self, parent: tk.Misc, title: str, prompt: str, choices: list[str], current: str = "",
                 on_done: Callable[[str], None] | None = None, heading: str = "Used in this meeting"):
        self._on_done = on_done
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.transient(parent.winfo_toplevel())
        self.win.resizable(False, False)
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        ttk.Label(frm, text=prompt, wraplength=int(300 * max(1.0, parent.winfo_fpixels("1i") / 96))).grid(
            row=0, column=0, sticky="w")
        self.name = tk.StringVar(value=current)
        self.entry = ttk.Entry(frm, textvariable=self.name, width=34)
        self.entry.grid(row=1, column=0, sticky="we", pady=(6, 8))
        self.list = None
        if choices:
            ttk.Label(frm, text=heading, foreground=theme.MUTED,
                      font=(theme.FONT_FAMILY, theme.FONT_SIZE_SMALL)).grid(row=2, column=0, sticky="w")
            self.list = tk.Listbox(frm, height=min(6, len(choices)), exportselection=False, activestyle="none")
            for c in choices:
                self.list.insert("end", c)
            self.list.grid(row=3, column=0, sticky="we", pady=(2, 8))
            self.list.bind("<<ListboxSelect>>", self._pick)
            self.list.bind("<Double-Button-1>", lambda e: self.accept())
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, sticky="e")
        self.ok_btn = ttk.Button(btns, text="Save name", command=self.accept)
        self.ok_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)
        self.win.bind("<Return>", lambda e: self.accept())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _place_over(self.win, parent)
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def _pick(self, _event=None) -> None:
        sel = self.list.curselection() if self.list is not None else ()
        if sel:
            self.name.set(self.list.get(sel[0]))
            self.entry.icursor("end")

    def accept(self) -> None:
        name = self.name.get().strip()
        if not name:
            return
        self.win.destroy()
        if self._on_done is not None:
            self._on_done(name)
