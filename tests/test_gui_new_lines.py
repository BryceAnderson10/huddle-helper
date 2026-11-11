"""The "↓ N new lines" button of the transcript view: shown while scrolled up during a call, never over the text."""

import tkinter as tk

from huddle_helper.transcript_model import TranscriptModel

from .test_gui_smoke import root, seg  # noqa: F401 - root is a fixture


def lines(first, n):
    return [seg(s, "mic", s * 1000, s * 1000 + 500, "You", f"line {s}.") for s in range(first, first + n)]


def make_view(root):
    """A view with 40 lines, more than fit, scrolled to the end."""
    from huddle_helper.gui.transcript_view import TranscriptView

    top = tk.Toplevel(root)
    top.geometry("700x300+40+40")
    view = TranscriptView(top)
    view.pack(fill="both", expand=True)
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    view.apply(m.apply_transcript(lines(1, 40)), m)
    top.update()
    return view, m


def add_lines(view, m, n):
    view.apply(m.apply_transcript(lines(view.row_count + 1, n)), m)
    view.update()


def scroll_up_and_add(view, m, n):
    view.text.yview_moveto(0.0)
    view.update()
    add_lines(view, m, n)


def newest_line_in_view(view):
    return bool(view.text.bbox("end-1c linestart"))


def rect(w):
    return w.winfo_rootx(), w.winfo_rooty(), w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height()


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_the_button_counts_new_lines_while_scrolled_up_and_jumps_to_the_newest(root):
    view, m = make_view(root)
    scroll_up_and_add(view, m, 2)
    assert view.new_button.winfo_ismapped() and view.new_button.cget("text") == "↓ 2 new lines"
    assert not view.at_bottom
    add_lines(view, m, 1)  # more lines while it already shows
    assert view.new_button.cget("text") == "↓ 3 new lines"

    view.new_button.invoke()
    view.update()
    assert view.at_bottom and not view.new_button.winfo_ismapped()
    assert view.plain_text().splitlines()[-1].endswith("line 43.") and newest_line_in_view(view)


def test_the_button_hides_once_the_user_scrolls_back_to_the_bottom(root):
    view, m = make_view(root)
    scroll_up_and_add(view, m, 1)
    assert view.new_button.cget("text") == "↓ 1 new line"
    view.text.yview_moveto(1.0)
    view.update()
    assert not view.new_button.winfo_ismapped()
    assert view.at_bottom and newest_line_in_view(view)


def test_the_button_covers_no_transcript_text(root):
    view, m = make_view(root)
    scroll_up_and_add(view, m, 3)
    assert view.new_button.winfo_ismapped()
    assert not overlaps(rect(view.new_button), rect(view.text))
