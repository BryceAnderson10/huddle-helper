"""The transcript text widget (plan 4.5, "Construction"). Thin Tk: what to show comes from
``logic`` and the model ops; this file only draws them.

One read-only ``tk.Text`` with tab stops for the time, speaker and text columns and ``lmargin2`` at
the text column so wrapped lines line up. Everything is text with tags, no embedded widgets.
Each row owns a mark ``r<n>`` at its start (right gravity: inserting at the mark pushes it along),
so a row spans from its own mark to the next row's mark.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from ..diarize import YOU
from ..transcript_model import GapRow, Insert, LineRow, MarkRow, Remove, Row, TranscriptModel, Update
from . import logic, theme

log = logging.getLogger(__name__)


class TranscriptView(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        root = self.winfo_toplevel()
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.f_base = tkfont.Font(root=root, family=theme.FONT_FAMILY, size=theme.FONT_SIZE)
        self.f_bold = tkfont.Font(root=root, family=theme.FONT_FAMILY, size=theme.FONT_SIZE, weight="bold")
        self.f_small = tkfont.Font(root=root, family=theme.FONT_FAMILY, size=theme.FONT_SIZE_SMALL)
        self.f_italic = tkfont.Font(root=root, family=theme.FONT_FAMILY, size=theme.FONT_SIZE_SMALL,
                                    slant="italic")
        pad = int(14 * self._scale)
        self.x_speaker = self.f_small.measure("00:00:00") + pad
        self.spk_w = self.f_bold.measure("n" * 15) + pad
        self.x_text = self.x_speaker + self.spk_w

        self.text = tk.Text(self, wrap="word", state="disabled", font=self.f_base, bg="white", fg=theme.TEXT,
                            relief="flat", borderwidth=0, highlightthickness=0, padx=int(10 * self._scale),
                            pady=int(6 * self._scale), cursor="arrow", takefocus=True, undo=False,
                            tabs=(self.x_speaker, self.x_text), selectbackground=theme.SELECTED_BG,
                            selectforeground=theme.TEXT, inactiveselectbackground=theme.SELECTED_BG)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self._on_yscroll)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self._configure_tags()
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.menu = tk.Menu(self.text, tearoff=False)
        self.menu.add_command(label="Copy", command=self.copy_selection)
        self.text.bind("<Button-3>", self._popup)
        self.text.bind("<Control-c>", lambda e: (self.copy_selection(), "break")[1])

        self.new_button = ttk.Button(self, command=self.jump_to_end)
        self.placeholder = ttk.Label(self, text="", foreground=theme.MUTED, font=self.f_base, anchor="center",
                                     justify="center", wraplength=int(420 * self._scale))

        self._rows: list[Row] = []
        self._ids: list[str] = []
        self._next_id = 0
        self._colors: dict[str, int] = {}
        self._follow = True
        self._new_lines = 0
        self._model: TranscriptModel | None = None

    # -- setup ----------------------------------------------------------------
    def _configure_tags(self) -> None:
        t = self.text
        sp = int(2 * self._scale)
        t.tag_configure("line", lmargin1=0, lmargin2=self.x_text, spacing1=sp, spacing3=sp)
        t.tag_configure("gaprow", lmargin1=self.x_speaker, lmargin2=self.x_speaker, spacing1=sp, spacing3=sp,
                        foreground=theme.GAP_FG, font=self.f_italic)
        t.tag_configure("markrow", lmargin1=0, lmargin2=self.x_speaker, spacing1=sp, spacing3=sp,
                        background=theme.MARK_BG)
        t.tag_configure("time", foreground=theme.MUTED, font=self.f_small)
        t.tag_configure("body", font=self.f_base)
        t.tag_configure("spk", font=self.f_bold)
        for kind, color in theme.KIND_COLORS.items():
            t.tag_configure(f"kind:{kind}", foreground=color, font=self.f_bold)
        t.tag_configure("kind:other", foreground=theme.KIND_COLORS["bookmark"], font=self.f_bold)
        t.tag_configure("changed", background=theme.CHANGED_BG)
        t.tag_raise("changed")
        t.tag_raise("sel")

    # -- scrolling ---------------------------------------------------------------
    def _on_yscroll(self, first, last) -> None:
        self.scroll.set(first, last)
        self._follow = float(last) >= 0.999
        if self._follow and self._new_lines:
            self._hide_new_button()

    @property
    def at_bottom(self) -> bool:
        return self._follow

    def jump_to_end(self) -> None:
        self.text.see("end")
        self.text.yview_moveto(1.0)
        self._follow = True
        self._hide_new_button()

    def scroll_to_top(self) -> None:
        self.text.yview_moveto(0.0)

    def _hide_new_button(self) -> None:
        self._new_lines = 0
        self.new_button.place_forget()

    def _note_new_lines(self, n: int) -> None:
        if n <= 0:
            return
        self._new_lines += n
        self.new_button.configure(text=f"↓ {self._new_lines} new line{'' if self._new_lines == 1 else 's'}")
        self.new_button.place(relx=1.0, rely=1.0, x=-int(24 * self._scale), y=-int(12 * self._scale),
                              anchor="se")

    def set_placeholder(self, text: str | None) -> None:
        if text:
            self.placeholder.configure(text=text)
            self.placeholder.place(relx=0.5, rely=0.4, anchor="center")
        else:
            self.placeholder.place_forget()

    # -- selection / clipboard -------------------------------------------------------
    def _popup(self, event) -> None:
        self.text.focus_set()
        self.menu.tk_popup(event.x_root, event.y_root)

    def copy_selection(self) -> None:
        try:
            chunk = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(chunk)

    # -- reading back (smoke test, debugging) ------------------------------------------
    def plain_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    @property
    def row_count(self) -> int:
        return len(self._rows)

    # -- applying ops -------------------------------------------------------------------
    def clear(self) -> None:
        self.text.configure(state="normal")
        self._clear()
        self.text.configure(state="disabled")
        self._rows, self._ids = [], []
        self._colors.clear()
        self._hide_new_button()
        self._model = None

    def _clear(self) -> None:
        for mid in self._ids:
            self.text.mark_unset(mid)
        self.text.delete("1.0", "end")
        self._ids = []
        self._rows = []

    def apply(self, ops, model: TranscriptModel, autoscroll: bool = True) -> None:
        """Replay model ops. A Reset anywhere means redraw everything from ``model.rows``."""
        if not ops:
            return
        self._model = model
        follow = self._follow and autoscroll
        top = self.text.yview()[0]
        new_lines = 0
        norm = logic.normalize_ops(ops)
        self.text.configure(state="normal")
        try:
            if norm is None:
                self._clear()
                for row in model.rows:
                    self._insert_row(len(self._rows), row)
            else:
                for op in norm:
                    if isinstance(op, Insert):
                        self._insert_row(op.index, op.row)
                        if isinstance(op.row, LineRow):
                            new_lines += 1
                    elif isinstance(op, Update):
                        self._update_row(op.index, op.row)
                    elif isinstance(op, Remove):
                        self._remove_row(op.index)
        finally:
            self.text.configure(state="disabled")
        if self._rows:
            self.set_placeholder(None)
        if norm is None:
            if follow:
                self.jump_to_end()
            else:
                self.text.yview_moveto(top)
            return
        if follow:
            self.text.see("end")
        elif autoscroll:
            self._note_new_lines(new_lines)

    # -- rows -------------------------------------------------------------------------------
    def _start(self, i: int) -> str:
        return self.text.index(self._ids[i]) if i < len(self._ids) else self.text.index("end-1c")

    def _end(self, i: int) -> str:
        return self.text.index(self._ids[i + 1]) if i + 1 < len(self._ids) else self.text.index("end-1c")

    def _insert_row(self, i: int, row: Row) -> None:
        at = self._start(i)
        mid = f"r{self._next_id}"
        self._next_id += 1
        self.text.insert(at, *self._segments(row))
        self.text.mark_set(mid, at)
        self._ids.insert(i, mid)
        self._rows.insert(i, row)

    def _update_row(self, i: int, row: Row) -> None:
        start, end = self._start(i), self._end(i)
        self.text.delete(start, end)
        self.text.insert(start, *self._segments(row))
        self.text.mark_set(self._ids[i], start)
        self._rows[i] = row

    def _remove_row(self, i: int) -> None:
        self.text.delete(self._start(i), self._end(i))
        self.text.mark_unset(self._ids[i])
        del self._ids[i], self._rows[i]

    def _fit(self, name: str) -> str:
        name = logic.short_name(name, 24)
        limit = self.spk_w - int(12 * self._scale)
        while len(name) > 2 and self.f_bold.measure(name) > limit:
            name = name[:-2].rstrip() + "…"
        return name

    def _color_for(self, row: LineRow) -> str:
        if row.source == "mic" and row.label == YOU and not row.name:
            return theme.speaker_color(0, is_you=True)
        idx = self._colors.setdefault(row.person_key, len(self._colors))
        return theme.speaker_color(idx)

    def _segments(self, row: Row) -> list:
        """Arguments for ``Text.insert`` after the index: text, tags, text, tags, ..."""
        model = self._model
        t = logic.line_time(model, row.start_ms) if model is not None else ""
        if isinstance(row, LineRow):
            tag = f"spk:{row.person_key}"
            self.text.tag_configure(tag, foreground=self._color_for(row))
            return [t, ("line", "time"), "\t", ("line",), self._fit(row.display), ("line", "spk", tag),
                    "\t", ("line",), row.text.strip() + "\n", ("line", "body")]
        if isinstance(row, GapRow):
            return ["\t" + logic.gap_text(row, model) + "\n", ("gaprow",)]
        kind_tag = f"kind:{row.kind}" if row.kind in theme.KIND_COLORS else "kind:other"
        symbol = theme.KIND_SYMBOLS.get(row.kind, theme.KIND_SYMBOLS["bookmark"])
        return [t, ("markrow", "time"), "\t", ("markrow",), f"{symbol} {logic.mark_text(row, model)}\n",
                ("markrow", kind_tag)]

    # -- highlight ----------------------------------------------------------------------------
    def highlight(self, keys) -> int:
        """Mark the lines with these ``LineRow.key`` values as changed. Returns how many."""
        n = 0
        self.text.configure(state="normal")
        for i, row in enumerate(self._rows):
            if isinstance(row, LineRow) and row.key in keys:
                self.text.tag_add("changed", self._start(i), self._end(i))
                n += 1
        self.text.configure(state="disabled")
        return n

    def clear_highlight(self) -> None:
        self.text.tag_remove("changed", "1.0", "end")
