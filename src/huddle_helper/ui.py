"""Small Tk dialogs: start recording, settings (with vocabulary and hotkeys). Naming speakers is in the Transcript window."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import attendees as attendees_mod, config, hotkeys, models, vocab
from .gui import logic

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


PlanFn = Callable[..., vocab.VocabPlan]  # AppController.plan_vocabulary


class _TermsBox:
    """Multi-line box of vocabulary terms with a live "N of M terms used" line. Thin Tk: the
    counting is vocab.plan, called through ``plan``."""

    def __init__(self, parent, *, height: int, initial: str, plan: PlanFn | None,
                 meeting_only: bool, wraplength: int, extra: Callable[[], list[str]] | None = None):
        self._plan = plan
        self._extra = extra  # more terms counted with these (the attendee names, when the box is ticked)
        self._meeting_only = meeting_only
        self.text = tk.Text(parent, height=height, width=44, wrap="word", undo=True)
        self.text.insert("1.0", initial)
        self.count = ttk.Label(parent, text="")
        self.dropped = ttk.Label(parent, text="", wraplength=wraplength, foreground="#a60")
        self.text.bind("<KeyRelease>", lambda e: self.refresh())

    def terms(self) -> list[str]:
        return vocab.parse_terms(self.text.get("1.0", "end"))

    def refresh(self, other_terms: list[str] | None = None) -> None:
        if self._plan is None:
            return
        terms = vocab.parse_terms(chr(10).join([*self.terms(), *(self._extra() if self._extra else [])]))
        # plan(settings_terms, meeting_terms): the Start box holds the meeting terms
        p = self._plan(other_terms or [], terms) if self._meeting_only else self._plan(terms)
        count, dropped = vocab.summary_lines(p)
        self.count.config(text=count)
        self.dropped.config(text=dropped)


class StartDialog:
    def __init__(self, root: tk.Tk, on_start: Callable[[str, list[str], list[str]], None],
                 plan: PlanFn | None = None, settings_terms: list[str] | None = None):
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Start recording")
        self.win.resizable(False, False)
        self._on_start = on_start
        self._settings_terms = settings_terms or []
        frm = ttk.Frame(self.win, padding=14)
        frm.grid()
        ttk.Label(frm, text="Meeting name").grid(row=0, column=0, sticky="w")
        self.title = tk.StringVar(value="")
        entry = ttk.Entry(frm, textvariable=self.title, width=44)
        entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        ttk.Label(frm, text="Who's on the call (optional; one per line or separated by commas)").grid(
            row=2, column=0, columnspan=2, sticky="w")
        self.attendees = tk.Text(frm, height=3, width=44, wrap="word", undo=True)
        self.attendees.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 2))
        ttk.Label(frm, text="They become one-click names, and keys 1-9 in the transcript window name the "
                            "latest new voice.", wraplength=330, foreground="#555").grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.names_as_terms = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Also give these names to speech recognition", variable=self.names_as_terms,
                        command=self._refresh_terms).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(frm, text="Off by default: it can make a name appear where nobody said it.",
                  wraplength=330, foreground="#555").grid(row=6, column=0, columnspan=2, sticky="w", padx=(22, 0))
        ttk.Label(frm, text="Extra terms for this meeting (one per line, optional)").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.terms = _TermsBox(frm, height=3, initial="", plan=plan, meeting_only=True, wraplength=330,
                               extra=self._name_terms)
        self.terms.text.grid(row=8, column=0, columnspan=2, sticky="we", pady=(2, 2))
        self.terms.count.grid(row=9, column=0, columnspan=2, sticky="w")
        self.terms.dropped.grid(row=10, column=0, columnspan=2, sticky="w")
        self.terms.text.bind("<KeyRelease>", lambda e: self._refresh_terms())
        self.attendees.bind("<KeyRelease>", lambda e: self._refresh_terms() if self.names_as_terms.get() else None)
        ttk.Label(frm, text=CONSENT_REMINDER, wraplength=330, foreground="#a33").grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(8, 12))
        ttk.Button(frm, text="Cancel", command=self.win.destroy).grid(row=12, column=0, sticky="e", padx=6)
        ttk.Button(frm, text="Start recording", command=self._start).grid(row=12, column=1, sticky="e")
        entry.bind("<Return>", lambda e: self._start())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        _center(self.win)
        entry.focus_set()

    def attendee_names(self) -> list[str]:
        return attendees_mod.parse_attendees(self.attendees.get("1.0", "end"))

    def _name_terms(self) -> list[str]:
        return self.attendee_names() if self.names_as_terms.get() else []

    def _refresh_terms(self) -> None:
        self.terms.refresh(self._settings_terms)

    def _start(self) -> None:
        title = self.title.get().strip() or "meeting"
        names = self.attendee_names()
        terms = attendees_mod.merge_terms(self.terms.terms(), names, self.names_as_terms.get())
        self.win.destroy()
        self._on_start(title, terms, names)


HotkeyResultsFn = Callable[[dict[str, str]], "tuple[dict[str, hotkeys.Registration], bool]"]
_LEVEL_COLORS = {"ok": "#047857", "warn": "#92400e", "error": "#b91c1c", "info": "#555555"}


class _HotkeysTab:
    """Settings → Hotkeys: one combo per action with its registration result and the AltGr warning.
    Thin Tk: the wording is ``logic.hotkey_row_status``, the checks are ``hotkeys.check_combo``."""

    def __init__(self, parent, settings: config.Settings, results_fn: HotkeyResultsFn | None):
        self._results_fn = results_fn
        self._results: dict[str, hotkeys.Registration] = {}
        self._live = False
        self._checked: dict[str, str] = {}  # action -> the text that was checked
        ttk.Label(parent, text="Global hotkeys work while another program has the focus. They are held only "
                               "while a recording is running. Leave a box empty to turn that hotkey off. "
                               "The window buttons and the tray menu always work.",
                  wraplength=520, foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.vars: dict[str, tk.StringVar] = {}
        self.status: dict[str, tk.Label] = {}
        for i, (name, default) in enumerate(hotkeys.DEFAULTS.items(), start=1):
            ttk.Label(parent, text=hotkeys.ACTION_LABELS[name]).grid(row=i, column=0, sticky="nw", pady=4,
                                                                   padx=(0, 10))
            var = tk.StringVar(value=settings.hotkeys.get(name, default))
            ttk.Entry(parent, textvariable=var, width=22).grid(row=i, column=1, sticky="nw", pady=4)
            lbl = tk.Label(parent, text="", anchor="w", justify="left", wraplength=300,
                           font=("Segoe UI", 9))
            lbl.grid(row=i, column=2, sticky="nw", padx=(10, 0), pady=4)
            self.vars[name], self.status[name] = var, lbl
            var.trace_add("write", lambda *_: self.refresh())
        btns = ttk.Frame(parent)
        btns.grid(row=len(hotkeys.DEFAULTS) + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Check", command=self.check).pack(side="left")
        ttk.Button(btns, text="Restore defaults", command=self.restore).pack(side="left", padx=8)
        ttk.Label(parent, text="Write them like Ctrl+Alt+B (Ctrl, Alt, Shift, Win and one key).",
                  foreground="#555").grid(row=len(hotkeys.DEFAULTS) + 2, column=0, columnspan=3, sticky="w",
                                         pady=(8, 0))
        self.check() if results_fn is not None else self.refresh()

    def texts(self) -> dict[str, str]:
        return {n: v.get().strip() for n, v in self.vars.items()}

    def check(self) -> None:
        """Ask Windows which of these can be registered (the recording's own result while one runs)."""
        if self._results_fn is None:
            return
        texts = self.texts()
        try:
            self._results, self._live = self._results_fn({n: t for n, t in texts.items()})
        except Exception:  # noqa: BLE001
            self._results, self._live = {}, False
        self._checked = texts
        self.refresh()

    def restore(self) -> None:
        for name, default in hotkeys.DEFAULTS.items():
            self.vars[name].set(default)

    def refresh(self) -> None:
        texts = self.texts()
        dups = hotkeys.find_duplicates(texts)
        for name, text in texts.items():
            check = hotkeys.check_combo(text)
            reg = self._results.get(name) if self._checked.get(name) == text else None
            other = dups.get(name)
            msg, level = logic.hotkey_row_status(check, reg, self._live, hotkeys.ACTION_LABELS.get(other) if other else None)
            self.status[name].configure(text=msg, fg=_LEVEL_COLORS[level])

    def validate(self) -> tuple[dict[str, str] | None, str]:
        """(canonical bindings, "") or (None, what is wrong)."""
        out: dict[str, str] = {}
        for name, text in self.texts().items():
            try:
                out[name] = hotkeys.normalize(text)
            except ValueError as exc:
                return None, f"{hotkeys.ACTION_LABELS[name]}: {exc}"
        dups = hotkeys.find_duplicates(out)
        if dups:
            name, other = next(iter(dups.items()))
            return None, (f"{hotkeys.ACTION_LABELS[name]} and {hotkeys.ACTION_LABELS[other]} have the same "
                          "combination.")
        return out, ""


class SettingsDialog:
    def __init__(self, root: tk.Tk, settings: config.Settings, devices: dict[str, list[str]],
                 on_save: Callable[[config.Settings], None], plan: PlanFn | None = None,
                 hotkey_results: HotkeyResultsFn | None = None):
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Settings")
        self.win.resizable(False, False)
        self._on_save = on_save
        self._orig = settings
        default = "(Windows default)"
        tabs = ttk.Notebook(self.win)
        tabs.grid(row=0, column=0, padx=8, pady=(8, 0))
        frm = ttk.Frame(tabs, padding=14)
        tabs.add(frm, text="General")
        vtab = ttk.Frame(tabs, padding=14)
        tabs.add(vtab, text="Vocabulary")
        htab = ttk.Frame(tabs, padding=14)
        tabs.add(htab, text="Hotkeys")
        self.hotkeys_tab = _HotkeysTab(htab, settings, hotkey_results)
        ttk.Label(vtab, text="Words Whisper should expect: acronyms, product names, project code "
                             "names. One per line, spelled as you want them written. Lines starting "
                             "with # are ignored. Changes apply from the next line, even during a "
                             "meeting.", wraplength=380, foreground="#555").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        self.vocab = _TermsBox(vtab, height=12, initial="\n".join(settings.vocabulary), plan=plan,
                               meeting_only=False, wraplength=380)
        self.vocab.text.grid(row=1, column=0, sticky="we")
        self.vocab.count.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.vocab.dropped.grid(row=3, column=0, sticky="w")
        self.vocab.refresh()
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
        self.open_window = tk.BooleanVar(value=settings.open_window_on_start)
        row("", ttk.Checkbutton(frm, text="Separate speakers in meeting audio", variable=self.diar_sys))
        row("", ttk.Checkbutton(frm, text="Separate speakers on my microphone (in-room meetings)",
                                variable=self.diar_mic))
        row("", ttk.Checkbutton(frm, text="Drop mic lines that echo the speakers (no headphones)",
                                variable=self.echo))
        row("", ttk.Checkbutton(frm, text="Keep audio after the final transcript", variable=self.keep))
        row("", ttk.Checkbutton(frm, text="Open the transcript window when recording starts",
                                variable=self.open_window))

        self.purge = tk.StringVar(value=str(settings.auto_purge_days))
        purge_frame = ttk.Frame(frm)
        ttk.Spinbox(purge_frame, from_=0, to=3650, textvariable=self.purge, width=6).pack(side="left")
        ttk.Label(purge_frame, text="days (0 = never)").pack(side="left", padx=6)
        row("Delete kept audio after", purge_frame)

        btns = ttk.Frame(self.win)
        btns.grid(row=1, column=0, sticky="e", padx=14, pady=(8, 12))
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
        bindings, problem = self.hotkeys_tab.validate()
        if bindings is None:
            messagebox.showerror("Huddle Helper", problem, parent=self.win)
            return
        s = config.Settings.from_dict(self._orig.to_dict())
        s.hotkeys = bindings
        s.mic_device = None if self.mic.get() == self._default else self.mic.get()
        s.output_device = None if self.out.get() == self._default else self.out.get()
        folder = self.folder.get().strip()
        s.meetings_dir = "" if Path(folder) == config.paths.default_meetings_dir() else folder
        s.whisper_model = self.model.get()
        s.diarize_system = self.diar_sys.get()
        s.diarize_mic = self.diar_mic.get()
        s.echo_dedupe = self.echo.get()
        s.keep_audio = self.keep.get()
        s.open_window_on_start = self.open_window.get()
        s.auto_purge_days = days
        s.vocabulary = self.vocab.terms()
        self.win.destroy()
        self._on_save(s)
