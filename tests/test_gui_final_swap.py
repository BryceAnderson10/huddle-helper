"""Where the transcript view lands after the live-to-final swap (#28): whole rows, never a top row cut off."""

import tkinter as tk

from huddle_helper.transcript_model import TranscriptModel

from .test_gui_smoke import root, seg  # noqa: F401 - root is a fixture


def lines(n, words=lambda s: 4):
    """Lines one per 5 s; ``words(s)`` sets how long line ``s`` is (long lines wrap)."""
    return [seg(s, "mic", s * 5000, s * 5000 + 4000, "You", f"line {s} " + "word " * words(s)) for s in range(1, n + 1)]


def make_view(root, height, live):
    from huddle_helper.gui.transcript_view import TranscriptView

    top = tk.Toplevel(root)
    top.geometry(f"700x{height}+40+40")
    view = TranscriptView(top)
    view.pack(fill="both", expand=True)
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    view.apply(m.apply_transcript(live), m)
    top.update()
    return view, m


def swap(view, m, final):
    view.apply(m.switch_to_final(final, {}).ops, m)
    view.update()


def inset(view):
    t = view.text
    return int(t.cget("pady")) + int(t.cget("borderwidth")) + int(t.cget("highlightthickness"))


def top_line_whole(view):
    return view.text.dlineinfo("@0,0")[1] >= inset(view)


def last_row_whole(view):
    # dlineinfo only measures the visible part of a line, so look past the newest row: when the blank line after
    # it is in view, all of it is.
    return view.text.dlineinfo("end-1c") is not None


def row_starting_at_top(view):
    """The row whose start is the first thing in view, or None when the view starts inside a row."""
    at = view.text.index(f"@0,{inset(view)}")
    for i, row in enumerate(view._rows):
        if view._start(i) == at:
            return row
    return None


def test_at_the_bottom_the_swap_stays_at_the_bottom_with_the_top_row_whole(root):
    for height in range(240, 280):  # two line heights: wherever the pixels fall
        view, m = make_view(root, height, lines(40))
        assert view.at_bottom
        swap(view, m, lines(37, words=lambda s: 4 + (s % 4) * 9))  # fewer lines, some wrapping
        assert (height, view.at_bottom, last_row_whole(view), top_line_whole(view)) == (height, True, True, True)
        view.winfo_toplevel().destroy()


def test_scrolled_up_the_swap_keeps_the_nearest_line_at_the_top_whole(root):
    view, m = make_view(root, 300, lines(40))
    view.text.yview(view._start(19))  # live line 20 (at 100 s) at the top...
    view.text.yview_moveto(view.text.yview()[0] + 0.003)  # ...cut by a few pixels
    view.update()
    assert not view.at_bottom and not top_line_whole(view)
    # The final pass drops the first 8 lines and starts each line 0.7 s later than the live one did.
    final = [dict(e, start_ms=e["start_ms"] + 700, end_ms=e["end_ms"] + 700) for e in lines(40)[8:]]
    swap(view, m, final)
    row = row_starting_at_top(view)
    assert row is not None and row.start_ms == 100_700
    assert top_line_whole(view) and not view.at_bottom


def test_scrolled_up_near_the_end_of_a_shorter_final_the_top_row_is_still_whole(root):
    for height in range(240, 280):
        view, m = make_view(root, height, lines(40))
        view.text.yview(view._start(19))
        view.update()
        assert not view.at_bottom
        swap(view, m, lines(24, words=lambda s: 4 + (s % 4) * 9))  # line 20 is now in the last screenful
        assert (height, top_line_whole(view), last_row_whole(view)) == (height, True, True)
        view.winfo_toplevel().destroy()


def test_a_bookmark_at_the_top_stays_at_the_top(root):
    from huddle_helper.transcript_model import MarkRow

    view, m = make_view(root, 300, lines(40))
    view.apply(m.apply_marks([{"type": "mark", "id": 1, "kind": "decision", "label": "Ship it", "at_ms": 100_000}]), m)
    i = next(i for i, r in enumerate(view._rows) if isinstance(r, MarkRow))  # right after line 20 (also at 100 s)
    view.text.yview(view._start(i))
    view.update()
    swap(view, m, lines(40))
    row = row_starting_at_top(view)
    assert isinstance(row, MarkRow) and row.id == 1
