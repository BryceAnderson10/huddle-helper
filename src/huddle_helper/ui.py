"""Small Tk dialogs: start recording, settings, rename speakers."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import config, meeting as mt, models
from .writer import read_events

CONSENT_REMINDER = ("Recording laws differ by place. Tell everyone in the meeting that you are "
                    "recording; complying is your responsibility.")


def _center(win: tk.Toplevel) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{x}+{y}")
    win.lift()
    win.attributes("-topmost", True)
    win.after(200, lambda: win.attributes("-topmost", False))
    win.focus_force()


class StartDialog:
    def __init__(self, root: tk.Tk, on_start: Callable[[str], None]):
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Start recording")
        self.win.resizable(False, False)
        self._on_start = on_start
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        ttk.Label(frm, text="Meeting name").grid(row=0, column=0, sticky="w")
        self.title = tk.StringVar(value="")
        entry = ttk.Entry(frm, textvariable=self.title, width=44)
        entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        ttk.Label(frm, text=CONSENT_REMINDER, wraplength=330, foreground="#a33").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Button(frm, text="Cancel", command=self.win.destroy).grid(row=3, column=0, sticky="e", padx=6)
        ttk.Button(frm, text="Start recording", command=self._start).grid(row=3, column=1, sticky="e")
        entry.bind("<Return>", lambda e: self._start())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _center(self.win)
        entry.focus_set()

    def _start(self) -> None:
        title = self.title.get().strip() or "meeting"
        self.win.destroy()
        self._on_start(title)


class SettingsDialog:
    def __init__(self, root: tk.Tk, settings: config.Settings, devices: dict[str, list[str]],
                 on_save: Callable[[config.Settings], None]):
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Settings")
        self.win.resizable(False, False)
        self._on_save = on_save
        self._orig = settings
        default = "(Windows default)"
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        r = 0

        def row(label, widget):
            nonlocal r
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="we", pady=3)
            r += 1

        self.mic = tk.StringVar(value=settings.mic_device or default)
        row("Microphone", ttk.Combobox(frm, textvariable=self.mic, state="readonly", width=46,
                                       values=[default, *devices.get("inputs", [])]))
        self.out = tk.StringVar(value=settings.output_device or default)
        row("Meeting audio from", ttk.Combobox(frm, textvariable=self.out, state="readonly", width=46,
                                               values=[default, *devices.get("outputs", [])]))
        self._default = default

        folder_frame = ttk.Frame(frm)
        self.folder = tk.StringVar(value=str(settings.resolved_meetings_dir()))
        ttk.Entry(folder_frame, textvariable=self.folder, width=38).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_frame, text="Browse…", command=self._browse).pack(side="left", padx=(6, 0))
        row("Output folder", folder_frame)

        self.model = tk.StringVar(value=settings.whisper_model)
        row("Speech model", ttk.Combobox(frm, textvariable=self.model, state="readonly", width=46,
                                         values=list(models.WHISPER_MODELS)))
        ttk.Label(frm, text="Larger models are more accurate but need a faster CPU. A new model "
                            "downloads once.", wraplength=360, foreground="#555").grid(
            row=r, column=1, sticky="w")
        r += 1

        self.diar_sys = tk.BooleanVar(value=settings.diarize_system)
        self.diar_mic = tk.BooleanVar(value=settings.diarize_mic)
        self.echo = tk.BooleanVar(value=settings.echo_dedupe)
        self.keep = tk.BooleanVar(value=settings.keep_audio)
        row("", ttk.Checkbutton(frm, text="Separate speakers in meeting audio", variable=self.diar_sys))
        row("", ttk.Checkbutton(frm, text="Separate speakers on my microphone (in-room meetings)",
                                variable=self.diar_mic))
        row("", ttk.Checkbutton(frm, text="Drop mic lines that echo the speakers (no headphones)",
                                variable=self.echo))
        row("", ttk.Checkbutton(frm, text="Keep audio after the final transcript", variable=self.keep))

        self.purge = tk.StringVar(value=str(settings.auto_purge_days))
        purge_frame = ttk.Frame(frm)
        ttk.Spinbox(purge_frame, from_=0, to=3650, textvariable=self.purge, width=6).pack(side="left")
        ttk.Label(purge_frame, text="days (0 = never)").pack(side="left", padx=6)
        row("Delete kept audio after", purge_frame)

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left")
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _center(self.win)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.win, initialdir=self.folder.get())
        if chosen:
            self.folder.set(chosen)

    def _save(self) -> None:
        try:
            days = max(0, int(self.purge.get()))
        except ValueError:
            messagebox.showerror("Huddle Helper", "Auto-delete days must be a whole number.", parent=self.win)
            return
        s = config.Settings.from_dict(self._orig.to_dict())
        s.mic_device = None if self.mic.get() == self._default else self.mic.get()
        s.output_device = None if self.out.get() == self._default else self.out.get()
        folder = self.folder.get().strip()
        s.meetings_dir = "" if Path(folder) == config.paths.default_meetings_dir() else folder
        s.whisper_model = self.model.get()
        s.diarize_system = self.diar_sys.get()
        s.diarize_mic = self.diar_mic.get()
        s.echo_dedupe = self.echo.get()
        s.keep_audio = self.keep.get()
        s.auto_purge_days = days
        self.win.destroy()
        self._on_save(s)


def speaker_samples(folder: mt.MeetingFolder, per_speaker: int = 2) -> dict[str, list[str]]:
    """Speaker id -> a few example lines, from the final transcript if present."""
    path = folder.final_path if folder.final_path.exists() else folder.live_path
    out: dict[str, list[str]] = {}
    if not path.exists():
        return out
    for e in read_events(path):
        if e.get("type") != "segment":
            continue
        lines = out.setdefault(e["speaker"], [])
        if len(lines) < per_speaker and len(e["text"]) > 12:
            lines.append(e["text"])
    return out


class SpeakersDialog:
    """Rename speakers (F9): stored as speakers.json in the meeting folder."""

    def __init__(self, root: tk.Tk, meetings_dir: Path):
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Rename speakers")
        self.meetings = [m for m in mt.iter_meetings(meetings_dir)][::-1][:30]
        frm = ttk.Frame(self.win, padding=14)
        frm.grid(sticky="nsew")
        if not self.meetings:
            ttk.Label(frm, text="No meetings yet.").grid()
            ttk.Button(frm, text="Close", command=self.win.destroy).grid(pady=(10, 0))
            _center(self.win)
            return
        ttk.Label(frm, text="Meeting").grid(row=0, column=0, sticky="w")
        self.choice = tk.StringVar(value=self.meetings[0].meeting_id)
        box = ttk.Combobox(frm, textvariable=self.choice, state="readonly", width=50,
                           values=[m.meeting_id for m in self.meetings])
        box.grid(row=0, column=1, sticky="we", pady=(0, 8))
        box.bind("<<ComboboxSelected>>", lambda e: self._load())
        self.rows = ttk.Frame(frm)
        self.rows.grid(row=1, column=0, columnspan=2, sticky="we")
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save names", command=self._save).pack(side="left")
        self.entries: dict[str, tk.StringVar] = {}
        self._load()
        _center(self.win)

    def _folder(self) -> mt.MeetingFolder:
        return next(m for m in self.meetings if m.meeting_id == self.choice.get())

    def _load(self) -> None:
        for child in self.rows.winfo_children():
            child.destroy()
        folder = self._folder()
        names = folder.read_speakers()
        samples = speaker_samples(folder)
        self.entries = {}
        if not samples:
            ttk.Label(self.rows, text="No transcript lines yet.").grid()
            return
        if not folder.final_path.exists():
            ttk.Label(self.rows, text="Final transcript not ready yet; these are the provisional live labels.",
                      foreground="#a60").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        for i, (spk, lines) in enumerate(sorted(samples.items()), start=1):
            ttk.Label(self.rows, text=spk, width=16).grid(row=i * 2, column=0, sticky="w")
            var = tk.StringVar(value=names.get(spk, ""))
            ttk.Entry(self.rows, textvariable=var, width=24).grid(row=i * 2, column=1, sticky="w", padx=6)
            self.entries[spk] = var
            sample = " / ".join(lines)[:110]
            ttk.Label(self.rows, text=f"“{sample}”", foreground="#555", wraplength=420).grid(
                row=i * 2 + 1, column=0, columnspan=3, sticky="w", pady=(0, 6))

    def _save(self) -> None:
        folder = self._folder()
        folder.write_speakers({k: v.get() for k, v in self.entries.items()})
        messagebox.showinfo("Huddle Helper", f"Saved speaker names to {folder.speakers_path.name}.",
                            parent=self.win)
