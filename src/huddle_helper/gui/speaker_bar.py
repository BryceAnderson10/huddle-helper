"""The speaker bar (plan 4.5): one chip per person in the meeting, under the notice bar. Thin Tk.

A chip is a coloured "● name" range in a small read-only ``tk.Text`` (it wraps by itself, so no flow
layout is needed). Clicking a chip calls ``on_chip(person_key, x_root, y_root)``; the window opens the
chip menu there ("Hear this voice" now; naming comes with WP8). What the chips are comes from
``logic.speaker_chips``.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from . import logic, theme


class SpeakerBar(tk.Text):
    MAX_LINES = 3

    def __init__(self, parent):
        root = parent.winfo_toplevel()
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self._font = tkfont.Font(root=root, family=theme.FONT_FAMILY, size=theme.FONT_SIZE_SMALL, weight="bold")
        super().__init__(parent, height=1, wrap="word", state="disabled", font=self._font, bg="white",
                         relief="flat", borderwidth=0, highlightthickness=0, cursor="arrow",
                         padx=int(10 * self._scale), pady=int(3 * self._scale), takefocus=False,
                         selectbackground="white", inactiveselectbackground="white")
        self.on_chip: Callable[[str, int, int], None] | None = None
        self._chips: list[logic.SpeakerChip] = []
        self._signature: tuple | None = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self.configure(cursor="hand2" if self._chip_at(e.x, e.y) is not None else "arrow"))

    def set_chips(self, chips: list[logic.SpeakerChip], color_of: Callable[[str], str]) -> None:
        """Redraw when the people (or their names) changed; ``color_of(person_key)`` is the colour the
        transcript uses for that person."""
        sig = tuple((c.person_key, c.display, c.unnamed) for c in chips)
        if sig == self._signature:
            return
        self._signature, self._chips = sig, list(chips)
        self.configure(state="normal")
        self.delete("1.0", "end")
        for i, chip in enumerate(chips):
            tag = f"chip{i}"
            self.tag_configure(tag, foreground=color_of(chip.person_key))
            if chip.unnamed:  # a new voice nobody has named: highlighted, and asks
                self.tag_configure(tag, background=theme.CHIP_NEW_BG)
            self.insert("end", "● ", (tag,))
            self.insert("end", chip.text, (tag,))
            if i < len(chips) - 1:
                self.insert("end", "     ")
        self.configure(state="disabled")
        self.update_idletasks()
        lines = self.count("1.0", "end-1c", "displaylines")
        self.configure(height=max(1, min(self.MAX_LINES, int(lines[0]) if lines else 1)))

    def _chip_at(self, x: int, y: int) -> int | None:
        idx = self.index(f"@{x},{y}")
        for i in range(len(self._chips)):
            r = self.tag_ranges(f"chip{i}")
            if r and self.compare(idx, ">=", r[0]) and self.compare(idx, "<", r[1]):
                return i
        return None

    def _click(self, event) -> str:
        i = self._chip_at(event.x, event.y)
        if i is not None and self.on_chip is not None:
            self.on_chip(self._chips[i].person_key, event.x_root, event.y_root)
        return "break"
