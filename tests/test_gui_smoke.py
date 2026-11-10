"""One Tk smoke test: build the view and window, apply ops, read the text back. Skipped without a display."""

import gc
import tkinter as tk
from types import SimpleNamespace

import pytest

from huddle_helper import meeting as mt
from huddle_helper.app import AppState
from huddle_helper.transcript_model import Reset, TranscriptModel

from .helpers import make_meeting


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    r.withdraw()
    yield r
    r.destroy()
    del r
    gc.collect()  # free Tk objects here, not on a worker thread of a later test


def seg(seq, source, start, end, speaker, text):
    return {"type": "segment", "seq": seq, "start_ms": start, "end_ms": end, "source": source,
            "speaker": speaker, "text": text, "confidence": 0.9}


def test_view_applies_ops_in_order_and_reads_back(root):
    from huddle_helper.gui.transcript_view import TranscriptView

    view = TranscriptView(root)
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    ops = m.apply_transcript([seg(1, "mic", 1000, 2000, "You", "first line."),
                              seg(2, "system", 5000, 6000, "SPEAKER_00", "third line.")])
    view.apply(ops, m)
    ops = m.apply_transcript([seg(3, "system", 3000, 4000, "SPEAKER_01", "second line.")])  # lands in the middle
    ops += m.apply_marks([{"type": "mark", "id": 1, "kind": "decision", "label": "Ship it", "at_ms": 5500}])
    ops += m.set_meta({"started_at": "2026-01-15T09:30:00+00:00",
                       "gaps": [{"source": "mic", "start_ms": 6500, "end_ms": 9500, "reason": "muted"}]})
    view.apply(ops, m)
    lines = view.plain_text().splitlines()
    assert [l.split("\t")[-1] for l in lines[:3]] == ["first line.", "second line.", "third line."]
    assert "Decision · Ship it" in lines[3] and "You muted your mic" in lines[4]
    assert lines[0].startswith("09:30:01\tYou\t") and view.row_count == 5
    # rename + remove through ops
    ops = m.apply_tags([{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Dana"}])
    ops += m.apply_tags([{"type": "tag", "scope": "line", "source": "system", "start_ms": 5000, "name": "Eric"}])
    view.apply(ops, m)
    text = view.plain_text()
    assert "\tDana\t" in text and "\tEric\t" in text and "SPEAKER_01" not in text
    view.apply(m.apply_marks([{"type": "mark_delete", "id": 1}]), m)
    assert "Decision" not in view.plain_text() and view.row_count == 4
    # a Reset redraws from the model, once
    view.apply([Reset()] + m.apply_transcript([seg(4, "mic", 20_000, 21_000, "You", "later.")]), m)
    assert view.plain_text().count("later.") == 1 and view.row_count == 5
    assert view.highlight({("system", 3000)}) == 1
    view.clear_highlight()


def test_window_follows_a_meeting_and_shows_header_and_status(root, tmp_path):
    from huddle_helper.gui.window import MainWindow

    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "hello."), ("system", 2000, 3000, "SPEAKER_00", "hi.")],
                          status=mt.STATUS_RECORDING, title="Smoke")
    ctl = SimpleNamespace(state=AppState(phase="ready"), settings=SimpleNamespace(
        resolved_meetings_dir=lambda: tmp_path), current_folder=lambda: None)
    w = MainWindow(root, ctl)
    w.show(folder)
    root.update()
    assert "hello." in w.view.plain_text() and "hi." in w.view.plain_text()
    assert w.head_text.cget("text").startswith("2026") or "line" in w.head_text.cget("text")
    assert "Provisional" in w.notice.cget("text")
    w._tick_slow()
    w.hide()
    assert not w.visible
    w.destroy()


def test_export_dialog_copies_the_chosen_format_to_the_clipboard(root, tmp_path):
    from huddle_helper.gui.dialogs import ExportDialog, MeetingPicker

    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "hello."), ("system", 2000, 3000, "SPEAKER_00", "hi.")],
                          final=[("mic", 0, 1000, "You", "hello."), ("system", 2000, 3000, "SPEAKER_00", "hi.")],
                          title="Export me")
    model = TranscriptModel.load(folder)
    dlg = ExportDialog(root, model)
    dlg.fmt.set("txt")
    dlg.times.set("elapsed")
    dlg.copy()
    clip = root.clipboard_get()
    assert "hello." in clip and "hi." in clip and "\r" not in clip and "Export me" in clip
    dlg.fmt.set("md")
    dlg.copy()
    assert root.clipboard_get().lstrip().startswith("# Export me")
    dlg.win.destroy()
    opened = []
    picker = MeetingPicker(root, tmp_path, opened.append)
    assert picker.box.size() == 1
    picker._open()
    assert opened and opened[0].meeting_id == folder.meeting_id
