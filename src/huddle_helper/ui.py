"""Small Tk dialogs: start recording, settings (with vocabulary and hotkeys). Naming speakers is in the Transcript window."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import attendees as attendees_mod, bookmark_types, config, hotkeys, models, outputs, vocab
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

    def set_terms(self, terms: list[str]) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n".join(terms))
        self.refresh()

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
    def __init__(self, root: tk.Tk, on_start: Callable[[str, list[str], list[str], bool], None],
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
        # L24: not remembered between meetings; every meeting starts with the box clear.
        self.start_muted = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Start with my mic muted", variable=self.start_muted).grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(frm, text="Only the call is recorded until you unmute. The call app still hears you.",
                  wraplength=330, foreground="#555").grid(row=12, column=0, columnspan=2, sticky="w", padx=(22, 0))
        ttk.Label(frm, text=CONSENT_REMINDER, wraplength=330, foreground="#a33").grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(8, 12))
        ttk.Button(frm, text="Cancel", command=self.win.destroy).grid(row=14, column=0, sticky="e", padx=6)
        ttk.Button(frm, text="Start recording", command=self._start).grid(row=14, column=1, sticky="e")
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
        muted = bool(self.start_muted.get())
        self.win.destroy()
        self._on_start(title, terms, names, muted)


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


class _TypesTab:
    """Settings -> Bookmark types (L28): up to four names, each with an optional hotkey. Thin Tk: the checks are
    ``bookmark_types.validate`` and ``hotkeys.check_combo``."""

    def __init__(self, parent, settings: config.Settings, other_hotkeys: Callable[[], dict[str, str]]):
        self._other = other_hotkeys
        ttk.Label(parent, text="Add your own kinds of bookmark, such as “Risk” or “Follow up with legal”. They "
                               "appear next to Decision, Action item and Question in the window, the tray menu "
                               "and the exports. A hotkey is optional; it works while recording, like the "
                               "others. Removing a type does not change meetings already recorded: their "
                               "bookmarks keep the name.", wraplength=520, foreground="#555").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(parent, text="Name").grid(row=1, column=1, sticky="w")
        ttk.Label(parent, text="Hotkey (optional)").grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.ids: list[str] = []
        self.names: list[tk.StringVar] = []
        self.keys: list[tk.StringVar] = []
        self.status: list[tk.Label] = []
        have = list(settings.custom_bookmark_types)
        for i in range(bookmark_types.MAX_TYPES):
            e = have[i] if i < len(have) else {"id": "", "name": "", "hotkey": ""}
            ttk.Label(parent, text=f"{i + 1}").grid(row=2 + i, column=0, sticky="nw", pady=4, padx=(0, 8))
            n, k = tk.StringVar(value=e["name"]), tk.StringVar(value=e["hotkey"])
            ttk.Entry(parent, textvariable=n, width=26).grid(row=2 + i, column=1, sticky="nw", pady=4)
            ttk.Entry(parent, textvariable=k, width=20).grid(row=2 + i, column=2, sticky="nw", pady=4, padx=(10, 0))
            lbl = tk.Label(parent, text="", anchor="w", justify="left", wraplength=250, font=("Segoe UI", 9))
            lbl.grid(row=2 + i, column=3, sticky="nw", padx=(10, 0), pady=4)
            self.ids.append(e["id"])
            self.names.append(n)
            self.keys.append(k)
            self.status.append(lbl)
            n.trace_add("write", lambda *_: self.refresh())
            k.trace_add("write", lambda *_: self.refresh())
        ttk.Label(parent, text=f"Up to {bookmark_types.MAX_TYPES} types, names up to {bookmark_types.NAME_MAX} "
                               "characters. Clear a row to remove it.", foreground="#555").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.refresh()

    def rows(self) -> list[tuple[str, str, str]]:
        return [(self.ids[i], self.names[i].get(), self.keys[i].get()) for i in range(len(self.ids))]

    def validate(self) -> tuple[list[dict] | None, str]:
        """(the types to save, "") or (None, what is wrong)."""
        entries, problems = bookmark_types.validate(self.rows(), self._other())
        if entries is None:
            p = problems[0]
            return None, f"Bookmark type {p.row + 1}: {p.text}"
        return entries, ""

    def refresh(self) -> None:
        _, problems = bookmark_types.validate(self.rows(), self._other())
        by_row: dict[int, str] = {}
        for p in problems:
            by_row.setdefault(p.row, p.text)
        for i, (_, name, key) in enumerate(self.rows()):
            if i in by_row:
                self.status[i].configure(text=by_row[i], fg=_LEVEL_COLORS["error"])
                continue
            check = hotkeys.check_combo(key)
            if check.altgr:
                self.status[i].configure(text=check.warning, fg=_LEVEL_COLORS["warn"])
            elif name.strip() or key.strip():
                self.status[i].configure(text="OK" if key.strip() else "OK, no hotkey", fg=_LEVEL_COLORS["ok"])
            else:
                self.status[i].configure(text="", fg=_LEVEL_COLORS["info"])


class SettingsDialog:
    def __init__(self, root: tk.Tk, settings: config.Settings, devices: dict[str, list[str]],
                 on_save: Callable[[config.Settings], None], plan: PlanFn | None = None,
                 hotkey_results: HotkeyResultsFn | None = None,
                 current: Callable[[], config.Settings] | None = None):
        """``current`` gives the settings as they are now (they can change while the dialog is open, e.g. a term
        added from a line); Save keeps the vocabulary terms added that way."""
        self.win = tk.Toplevel(root)
        self.win.title("Huddle Helper - Settings")
        self.win.resizable(False, False)
        self._on_save = on_save
        self._orig = settings
        self._current = current
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
        ttab = ttk.Frame(tabs, padding=14)
        tabs.add(ttab, text="Bookmark types")
        self._tabs, self._types_page = tabs, ttab
        self.types_tab = _TypesTab(ttab, settings, self.hotkeys_tab.texts)
        for var in self.hotkeys_tab.vars.values():  # a hotkey typed in the other tab may now clash
            var.trace_add("write", lambda *_: self.types_tab.refresh())
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
        # L38: share the list as a plain text file, one term per line. Nothing is saved until Save is pressed.
        files = ttk.Frame(vtab)
        files.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Button(files, text="Add from file…", command=lambda: self._pick_import(False)).pack(side="left")
        ttk.Button(files, text="Replace from file…", command=lambda: self._pick_import(True)).pack(
            side="left", padx=(6, 0))
        ttk.Button(files, text="Export to file…", command=self._pick_export).pack(side="left", padx=(6, 0))
        self.vocab_note = ttk.Label(vtab, text="A .txt file with one term per line, to share a list with your team.",
                                    wraplength=380, foreground="#555")
        self.vocab_note.grid(row=5, column=0, sticky="w", pady=(4, 0))
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
        outs = devices.get("outputs", [])
        self.clip_out = tk.StringVar(value=outputs.choice_from_setting(settings.clip_output_device, outs, default))
        self.clip_out_box = ttk.Combobox(frm, textvariable=self.clip_out, state="readonly", width=46,
                                         values=outputs.choice_values(outs, settings.clip_output_device, default))
        row("Play clips on", self.clip_out_box)

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

        final_frame = ttk.Frame(frm)
        self.final_on = tk.BooleanVar(value=settings.final_transcribe)
        self.final_model = tk.StringVar(value=settings.final_model)
        ttk.Checkbutton(final_frame, text="Improve the transcript after the meeting (slower), with",
                        variable=self.final_on).pack(side="left")
        ttk.Combobox(final_frame, textvariable=self.final_model, state="readonly", width=10,
                     values=[m for m in models.WHISPER_MODELS if m != "tiny.en"]).pack(side="left", padx=(6, 0))
        row("", final_frame)
        ttk.Label(frm, text="After Stop, the kept audio is transcribed a second time with this model. "
                            "The final transcript takes longer to arrive.", wraplength=360,
                  foreground="#555").grid(row=r, column=1, sticky="w")
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

    # -- vocabulary file (L38) -------------------------------------------------------------------------
    def import_terms(self, path: str | Path, replace: bool = False) -> str:
        """Read a vocabulary file into the box (the settings are saved with Save, not here). Adding skips terms
        already in the list (ignoring case); replacing swaps the whole list. Returns the message shown."""
        try:
            imported = vocab.read_terms_file(path)
        except vocab.VocabFileError as exc:
            return self._vocab_say(str(exc), error=True)
        if not imported:
            return self._vocab_say("That file has no terms (blank lines and lines starting with # are ignored).",
                                   error=True)
        result = vocab.merge_import(self.vocab.terms(), imported, replace=replace)
        self.vocab.set_terms(result.terms)
        return self._vocab_say(result.message + " Press Save to keep the change.")

    def export_terms(self, path: str | Path) -> str:
        """Write the terms in the box (saved or not) to a text file, one per line, UTF-8."""
        terms = self.vocab.terms()
        if not terms:
            return self._vocab_say("There are no terms to export.", error=True)
        try:
            n = vocab.write_terms_file(path, terms)
        except vocab.VocabFileError as exc:
            return self._vocab_say(str(exc), error=True)
        return self._vocab_say(f"Saved {n} term{'' if n == 1 else 's'} to {path}")

    def _vocab_say(self, text: str, error: bool = False) -> str:
        self.vocab_note.configure(text=text, foreground="#b91c1c" if error else "#047857")
        return text

    def _pick_import(self, replace: bool) -> None:
        path = filedialog.askopenfilename(parent=self.win, title="Vocabulary file",
                                          filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.import_terms(path, replace)

    def _pick_export(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.win, title="Save vocabulary", defaultextension=".txt",
                                            initialfile="vocabulary.txt", filetypes=[("Text files", "*.txt")])
        if path:
            self.export_terms(path)

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
        types, problem = self.types_tab.validate()
        if types is None:
            self._tabs.select(self._types_page)
            messagebox.showerror("Huddle Helper", problem, parent=self.win)
            return
        s = config.Settings.from_dict(self._orig.to_dict())
        s.hotkeys = bindings
        s.custom_bookmark_types = types
        s.mic_device = None if self.mic.get() == self._default else self.mic.get()
        s.output_device = None if self.out.get() == self._default else self.out.get()
        s.clip_output_device = outputs.choice_to_setting(self.clip_out.get(), self._orig.clip_output_device,
                                                         self._default)
        folder = self.folder.get().strip()
        s.meetings_dir = "" if Path(folder) == config.paths.default_meetings_dir() else folder
        s.whisper_model = self.model.get()
        s.final_transcribe = self.final_on.get()
        s.final_model = self.final_model.get()
        s.diarize_system = self.diar_sys.get()
        s.diarize_mic = self.diar_mic.get()
        s.echo_dedupe = self.echo.get()
        s.keep_audio = self.keep.get()
        s.open_window_on_start = self.open_window.get()
        s.auto_purge_days = days
        now = self._current() if self._current is not None else self._orig
        s.vocabulary = vocab.keep_added(self.vocab.terms(), self._orig.vocabulary, now.vocabulary)
        self.win.destroy()
        self._on_save(s)
