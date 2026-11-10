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
from typing import Callable

from .. import playback
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
        self.x_time = self.f_small.measure(theme.STOP_GLYPH) + pad  # the play control comes first
        self.x_speaker = self.x_time + self.f_small.measure("00:00:00") + pad
        self.spk_w = self.f_bold.measure("n" * 15) + pad
        self.x_text = self.x_speaker + self.spk_w

        self.text = tk.Text(self, wrap="word", state="disabled", font=self.f_base, bg="white", fg=theme.TEXT,
                            relief="flat", borderwidth=0, highlightthickness=0, padx=int(10 * self._scale),
                            pady=int(6 * self._scale), cursor="arrow", takefocus=True, undo=False,
                            tabs=(self.x_time, self.x_speaker, self.x_text), selectbackground=theme.SELECTED_BG,
                            selectforeground=theme.TEXT, inactiveselectbackground=theme.SELECTED_BG)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self._on_yscroll)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self._configure_tags()
        self.menu = tk.Menu(self.text, tearoff=False)
        self.text.bind("<Button-3>", self._popup)
        self.text.bind("<Button-1>", self._click)
        # Set by the window (WP5): what a click on a bookmark row and the line menu do.
        self.on_edit_mark: Callable[[MarkRow], None] | None = None
        self.on_delete_mark: Callable[[MarkRow], None] | None = None
        self.on_bookmark_line: Callable[[LineRow, str], None] | None = None
        self.bookmark_items: Callable[[], list[logic.BookmarkItem]] = lambda: []
        # Naming (WP8), set by the window: "This line is…" gets a name, "" to clear the line's own, None to ask.
        self.on_name_line: Callable[[LineRow, str | None], None] | None = None
        self.line_name_menu: Callable[[LineRow], logic.LineNameMenu] = lambda row: logic.LineNameMenu(False)
        self.text.bind("<Control-c>", lambda e: (self.copy_selection(), "break")[1])
        # Click to hear (WP7), set by the window: play a row, and the "Hear this voice" menu entry.
        self.on_play: Callable[[Row], None] | None = None
        self.on_hear_voice: Callable[[str], None] | None = None
        self.on_play_from: Callable[[LineRow], None] | None = None  # "Play from here" (L19)
        # Play from here scrolls the heard line into view until the user scrolls themselves (wheel, scrollbar,
        # keys): their position is then left alone for the rest of the run.
        self._run_follow = True
        self._in_run = False
        for seq in ("<MouseWheel>", "<Prior>", "<Next>", "<Up>", "<Down>", "<Home>", "<End>"):
            self.text.bind(seq, self._user_scrolled, add="+")
        self.scroll.bind("<ButtonPress-1>", self._user_scrolled, add="+")
        self.voice_state: Callable[[str], tuple[bool, str]] = lambda key: (False, "")
        self.text.bind("<space>", self._space)
        self.text.bind("<Motion>", self._motion)
        self.text.bind("<Leave>", lambda e: self._hide_tip())
        self._ctx: playback.AudioContext | None = None
        self._ctx_sig: tuple | None = None
        self._playing_key: str | None = None
        self._selected: tuple[str, int] | None = None  # LineRow.key of the selected line
        self._tip: tk.Toplevel | None = None
        self._tip_for: str | None = None

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
        t.tag_configure("markedit", foreground=theme.MUTED, font=self.f_small, underline=True)
        t.tag_bind("markrow", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("markrow", "<Leave>", lambda e: t.configure(cursor="arrow"))
        t.tag_configure("time", foreground=theme.MUTED, font=self.f_small)
        t.tag_configure("body", font=self.f_base)
        t.tag_configure("spk", font=self.f_bold)
        for kind, color in theme.KIND_COLORS.items():
            t.tag_configure(f"kind:{kind}", foreground=color, font=self.f_bold)
        t.tag_configure("kind:other", foreground=theme.CUSTOM_COLOR, font=self.f_bold)  # user-defined types
        t.tag_configure("changed", background=theme.CHANGED_BG)
        t.tag_configure("play", font=self.f_small)
        t.tag_configure("play-on", foreground=theme.PLAY_ON)
        t.tag_configure("play-off", foreground=theme.PLAY_OFF)
        t.tag_configure("selrow", background=theme.SELECTED_BG)
        t.tag_configure("playing", background=theme.PLAYING_BG)
        t.tag_raise("selrow")
        t.tag_raise("playing")
        t.tag_raise("changed")
        t.tag_raise("sel")

    # -- scrolling ---------------------------------------------------------------
    def _on_yscroll(self, first, last) -> None:
        self.scroll.set(first, last)
        self._follow = float(last) >= 0.999
        if self._follow and self._new_lines:
            self._hide_new_button()

    def _user_scrolled(self, event=None) -> None:
        self._run_follow = False

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
    def row_at(self, x: int, y: int) -> tuple[int, Row] | None:
        """The row under a point in the text widget, None over empty space."""
        if not self._ids:
            return None
        idx = self.text.index(f"@{x},{y}")
        box = self.text.bbox(idx)
        if box is None or not (box[1] - 2 <= y <= box[1] + box[3] + 2):
            return None
        lo, hi = 0, len(self._ids)  # last row whose mark is at or before idx
        while lo < hi:
            mid = (lo + hi) // 2
            if self.text.compare(self._ids[mid], "<=", idx):
                lo = mid + 1
            else:
                hi = mid
        return (lo - 1, self._rows[lo - 1]) if lo > 0 else None

    def _on_glyph(self, x: int) -> bool:
        return x < int(self.text.cget("padx")) + self.x_time

    def _click(self, event) -> str | None:
        self.text.focus_set()
        hit = self.row_at(event.x, event.y)
        if hit is None:
            return None
        row = hit[1]
        if logic.row_play_key(row) is not None and self._on_glyph(event.x) and self.on_play is not None:
            self.after_idle(lambda: self.on_play(row))
            return "break"
        if isinstance(row, MarkRow) and self.on_edit_mark is not None:
            self.after_idle(lambda: self.on_edit_mark(row))  # not inside the click: the dialog grabs input
            return "break"
        if isinstance(row, LineRow):
            self.select(row)
        return None

    # -- selection (Space replays it) and the play control's tooltip -------------------------------
    def select(self, row: LineRow | None) -> None:
        self._selected = row.key if row is not None else None
        self._restyle_selected()

    @property
    def selected_row(self) -> LineRow | None:
        if self._selected is None:
            return None
        return next((r for r in self._rows if isinstance(r, LineRow) and r.key == self._selected), None)

    def _restyle_selected(self) -> None:
        self.text.tag_remove("selrow", "1.0", "end")
        for i, r in enumerate(self._rows):
            if isinstance(r, LineRow) and r.key == self._selected:
                self.text.tag_add("selrow", self._start(i), self._end(i))
                return

    def _space(self, event) -> str:
        row = self.selected_row
        if row is not None and self.on_play is not None:
            self.on_play(row)
        return "break"

    def _motion(self, event) -> None:
        hit = self.row_at(event.x, event.y) if self._ctx is not None else None
        key = logic.row_play_key(hit[1]) if hit else None
        if hit is None or key is None or not self._on_glyph(event.x):
            self.text.configure(cursor="arrow")
            self._hide_tip()
            return
        st = logic.row_play_state(self._ctx, hit[1])
        self.text.configure(cursor="hand2" if st.enabled else "arrow")
        if st.enabled:
            self._hide_tip()
        elif self._tip_for != key:
            self._hide_tip()
            self._tip_for = key
            tip = self._tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=st.reason, justify="left", wraplength=int(300 * self._scale), background="#ffffe1",
                     relief="solid", borderwidth=1, font=self.f_small, padx=6, pady=3).pack()
            tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 16}")

    def _hide_tip(self) -> None:
        self._tip_for = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _popup(self, event) -> None:
        self.text.focus_set()
        hit = self.row_at(event.x, event.y)
        menu = self.build_menu(hit[1] if hit else None)
        menu.tk_popup(event.x_root, event.y_root)

    def build_menu(self, row: Row | None) -> tk.Menu:
        """The right-click menu for ``row`` (a line, a bookmark or empty space)."""
        menu = self.menu
        menu.delete(0, "end")
        key = logic.row_play_key(row) if row is not None else None
        if key is not None and self.on_play is not None:
            st = logic.row_play_state(self._ctx, row) if self._ctx is not None else playback.PlayState(False)
            playing = key == self._playing_key
            label = "Stop" if playing else ("Play from 10 s before" if isinstance(row, MarkRow) else "Play")
            menu.add_command(label=label, command=lambda r=row: self.on_play(r),
                             state="normal" if (st.enabled or playing) else "disabled")
        if isinstance(row, LineRow) and self.on_play_from is not None:
            st = logic.row_play_state(self._ctx, row) if self._ctx is not None else playback.PlayState(False)
            menu.add_command(label="Play from here", command=lambda r=row: self.on_play_from(r),
                             state="normal" if st.enabled else "disabled")
        if isinstance(row, LineRow) and self.on_hear_voice is not None:
            ok, why = self.voice_state(row.person_key)
            menu.add_command(label=f"Hear this voice ({logic.short_name(row.display, 24)})",
                             command=lambda k=row.person_key: self.on_hear_voice(k),
                             state="normal" if ok else "disabled")
            if not ok and why:
                menu.add_command(label=logic.short_name(why, 60), state="disabled")
        if menu.index("end") is not None and isinstance(row, LineRow):
            menu.add_separator()
        if isinstance(row, LineRow) and self.on_name_line is not None:
            nm = self.line_name_menu(row)
            if not nm.enabled:
                menu.add_command(label="This line is…", state="disabled")
                if nm.note:
                    menu.add_command(label=nm.note, state="disabled")
            else:
                sub = tk.Menu(menu, tearoff=False)
                for name in nm.choices:
                    sub.add_command(label=logic.short_name(name, 30),
                                    command=lambda n=name, r=row: self.on_name_line(r, n))
                if nm.choices:
                    sub.add_separator()
                sub.add_command(label="Someone else…", command=lambda r=row: self.on_name_line(r, None))
                if nm.can_clear:
                    sub.add_command(label="Back to the speaker's name", command=lambda r=row: self.on_name_line(r, ""))
                menu.add_cascade(label="This line is…", menu=sub)
        if isinstance(row, LineRow) and self.on_bookmark_line is not None:
            sub = tk.Menu(menu, tearoff=False)
            for item in self.bookmark_items():
                sub.add_command(label=item.label, command=lambda k=item.kind, r=row: self.on_bookmark_line(r, k))
            menu.add_cascade(label="Bookmark this line as…", menu=sub)
        elif isinstance(row, MarkRow) and self.on_edit_mark is not None:
            menu.add_command(label="Edit bookmark…", command=lambda r=row: self.on_edit_mark(r))
            if self.on_delete_mark is not None:
                menu.add_command(label="Delete bookmark", command=lambda r=row: self.on_delete_mark(r))
        if menu.index("end") is not None:
            menu.add_separator()
        menu.add_command(label="Copy", command=self.copy_selection)
        return menu

    def copy_selection(self) -> None:
        try:
            chunk = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(logic.copyable(chunk))

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
        self._mark_row_state(i, row)

    def _mark_row_state(self, i: int, row: Row) -> None:
        """A (re)drawn row that is the playing one, or the selected line, gets its background."""
        key = logic.row_play_key(row)
        if key is not None and key == self._playing_key:
            self.text.tag_add("playing", self._start(i), self._end(i))
        if isinstance(row, LineRow) and row.key == self._selected:
            self.text.tag_add("selrow", self._start(i), self._end(i))

    def _update_row(self, i: int, row: Row) -> None:
        start, end = self._start(i), self._end(i)
        self.text.delete(start, end)
        self.text.insert(start, *self._segments(row))
        self.text.mark_set(self._ids[i], start)
        self._rows[i] = row
        self._mark_row_state(i, row)

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
            return [self._glyph(row), self._glyph_tags(row, "line"), "\t", ("line",), t, ("line", "time"),
                    "\t", ("line",), self._fit(row.display), ("line", "spk", tag),
                    "\t", ("line",), row.text.strip() + "\n", ("line", "body")]
        if isinstance(row, GapRow):
            return ["\t" + logic.gap_text(row, model) + "\n", ("gaprow",)]
        kind_tag = f"kind:{row.kind}" if row.kind in theme.KIND_COLORS else "kind:other"
        symbol = theme.KIND_SYMBOLS.get(row.kind, theme.CUSTOM_SYMBOL)
        return [self._glyph(row), self._glyph_tags(row, "markrow"), "\t", ("markrow",), t, ("markrow", "time"),
                "\t", ("markrow",), f"{symbol} {logic.mark_text(row, model)}",
                ("markrow", kind_tag), "  [edit]", ("markrow", "markedit"), "\n", ("markrow",)]

    # -- play controls (WP7) ----------------------------------------------------------------------
    def _glyph(self, row: Row) -> str:
        return theme.STOP_GLYPH if logic.row_play_key(row) == self._playing_key else theme.PLAY_GLYPH

    def _glyph_tags(self, row: Row, base: str) -> tuple[str, ...]:
        enabled = self._ctx is not None and logic.row_play_state(self._ctx, row).enabled
        return (base, "play", "play-on" if enabled else "play-off")

    def set_audio(self, ctx: playback.AudioContext, playing_key: str | None, run: bool = False) -> None:
        """Tell the view where audio can be read from and which clip is playing; restyles the play
        controls when either changed (greyed out or not, play or stop, the playing row's background)."""
        if ctx.signature != self._ctx_sig:
            self._ctx, self._ctx_sig = ctx, ctx.signature
            self._restyle_all_glyphs()
        else:
            self._ctx = ctx
        self.set_playing(playing_key, run)

    def set_playing(self, key: str | None, run: bool = False) -> None:
        """``key`` is the clip being heard; ``run``: it belongs to a Play from here run, whose highlight is
        scrolled into view unless the user scrolled away since the run began."""
        if run and not self._in_run:
            self._run_follow = True  # a new run: the clicked line is on screen, follow from there
        self._in_run = run and key is not None
        if key == self._playing_key:
            return
        old, self._playing_key = self._playing_key, key
        self.text.configure(state="normal")
        try:
            for i, row in enumerate(self._rows):
                rk = logic.row_play_key(row)
                if rk is not None and (rk == old or rk == key):
                    self._restyle_glyph(i, row)
                    if rk == key:
                        self.text.tag_add("playing", self._start(i), self._end(i))
                    else:
                        self.text.tag_remove("playing", self._start(i), self._end(i))
        finally:
            self.text.configure(state="disabled")
        if run and key is not None and self._run_follow:
            self.reveal(key)

    def reveal(self, key: str) -> None:
        """Scroll the row with this play key into view (nothing moves when it is already visible)."""
        for i, row in enumerate(self._rows):
            if logic.row_play_key(row) == key:
                self.text.see(self._end(i) + "-1c")
                self.text.see(self._start(i))
                return

    def _restyle_glyph(self, i: int, row: Row) -> None:
        """Replace the first character of row ``i`` (its play control) in place; the row mark stays."""
        start = self._start(i)
        base = "markrow" if isinstance(row, MarkRow) else "line"
        self.text.delete(start, f"{start}+1c")
        self.text.insert(start, self._glyph(row), self._glyph_tags(row, base))
        self.text.mark_set(self._ids[i], start)

    def _restyle_all_glyphs(self) -> None:
        t = self.text
        t.tag_remove("play-on", "1.0", "end")
        t.tag_remove("play-off", "1.0", "end")
        for i, row in enumerate(self._rows):
            if logic.row_play_key(row) is None:
                continue
            start = self._start(i)
            enabled = self._ctx is not None and logic.row_play_state(self._ctx, row).enabled
            t.tag_add("play-on" if enabled else "play-off", start, f"{start}+1c")

    def color_of(self, row: LineRow) -> str:
        return self._color_for(row)

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
