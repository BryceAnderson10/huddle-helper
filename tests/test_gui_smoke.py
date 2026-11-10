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


# -- bookmarks and mute (WP5) ---------------------------------------------------------------------
def test_mark_rows_show_an_edit_hint_open_the_editor_on_click_and_have_a_line_menu(root):
    from huddle_helper.gui import logic
    from huddle_helper.gui.transcript_view import TranscriptView

    top = tk.Toplevel(root)
    top.geometry("700x300+40+40")
    view = TranscriptView(top)
    view.pack(fill="both", expand=True)
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    ops = m.apply_transcript([seg(1, "mic", 1000, 2000, "You", "first line."),
                              seg(2, "system", 5000, 6000, "SPEAKER_00", "second line.")])
    ops += m.apply_marks([{"type": "mark", "id": 4, "kind": "decision", "at_ms": 3000},
                          {"type": "mark_label", "id": 4, "label": "Ship it"}])
    view.apply(ops, m)
    top.update()
    lines = view.plain_text().splitlines()
    assert "Decision · Ship it  [edit]" in lines[1] and lines[0].endswith("first line.")

    edited, deleted, lined = [], [], []
    view.on_edit_mark = edited.append
    view.on_delete_mark = deleted.append
    view.on_bookmark_line = lambda row, kind: lined.append((row.seq, kind))
    view.bookmark_items = lambda: logic.bookmark_items({"decision": "Ctrl+Alt+D"})

    def where(row_index):  # pixel position inside a row
        x, y, w, h = view.text.bbox(f"{row_index + 1}.12")
        return SimpleNamespace(x=x + 2, y=y + h // 2)

    index, row = view.row_at(where(1).x, where(1).y)
    assert index == 1 and row.id == 4
    assert view.row_at(where(0).x, where(0).y)[1].seq == 1
    assert view.row_at(5, view.text.winfo_height() - 3) is None  # empty space below the last row
    assert view._click(where(1)) == "break"  # a click on the bookmark: the editor, once the click is over
    assert view._click(where(0)) is None  # a click on a line is left to Tk (selection)
    top.update()
    assert [r.id for r in edited] == [4]

    def labels(menu):
        return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]

    assert labels(view.build_menu(m.rows[0])) == ["Bookmark this line as…", "Copy"]
    sub = view.menu.nametowidget(view.menu.entrycget(0, "menu"))
    assert labels(sub) == ["⚑ Bookmark", "✔ Decision", "☐ Action item", "? Question"]
    sub.invoke(1)
    assert lined == [(1, "decision")]
    assert labels(view.build_menu(m.rows[1])) == ["Edit bookmark…", "Delete bookmark", "Copy"]
    view.menu.invoke(1)
    assert [r.id for r in deleted] == [4]
    assert labels(view.build_menu(None)) == ["Copy"]
    top.destroy()


class FakeApp:
    """What the window asks of AppController for bookmarks and mute."""

    def __init__(self, tmp_path, state, folder=None):
        self.state = state
        self.folder = folder
        self.calls = []
        self.muted = False
        self.settings = SimpleNamespace(resolved_meetings_dir=lambda: tmp_path,
                                        hotkeys={"mute": "Ctrl+Alt+M", "decision": "Ctrl+Alt+D"})

    def current_folder(self):
        return self.folder

    def bookmark(self, kind="bookmark", via="hotkey", **kw):
        self.calls.append(("bookmark", kind, via, kw))
        return {"action": "added", "id": 1, "kind": kind, "at_ms": 1}

    def edit_mark(self, folder, mark_id, **kw):
        self.calls.append(("edit", mark_id, kw))
        return True

    def toggle_mic_muted(self):
        self.muted = not self.muted
        self.state.recording.mic_muted = self.muted
        self.calls.append(("mute", self.muted))
        return self.muted


def test_window_shows_the_mute_bar_split_button_and_calls_the_controller(root, tmp_path):
    from huddle_helper.gui import logic
    from huddle_helper.gui.window import MainWindow
    from huddle_helper.recorder import Status

    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "hello."), ("system", 4000, 5000, "SPEAKER_00", "hi.")],
                          status=mt.STATUS_RECORDING, title="Smoke")
    state = AppState(phase="recording", last_meeting=folder.meeting_id)
    state.recording = Status(state="recording", meeting_id=folder.meeting_id, elapsed_s=61, segments=2)
    ctl = FakeApp(tmp_path, state, folder)
    w = MainWindow(root, ctl)
    w.show(folder)
    root.update()
    assert w.mute_btn.winfo_ismapped() and w.mute_btn.cget("text") == "Mute me in the recording"
    assert not w.mute_bar.winfo_ismapped()
    assert w.bookmark_btn.winfo_ismapped() and w.bookmark_arrow.winfo_ismapped() and w.stop_btn.winfo_ismapped()
    assert w.bookmark_menu.index("end") == 3 and "Ctrl+Alt+D" in w.bookmark_menu.entrycget(1, "label")
    assert w.mic_lbl.cget("text").startswith("Mic ") and "muted" not in w.mic_lbl.cget("text")

    w.mute_btn.invoke()
    root.update()
    assert ctl.calls[-1] == ("mute", True)
    assert w.mute_bar.winfo_ismapped() and "MIC MUTED" in w.mute_bar.winfo_children()[0].cget("text")
    assert w.mute_btn.cget("text") == "Unmute me" and w.mic_lbl.cget("text") == "Mic muted"
    w._set_notice(logic.NoticeView("Provisional", "info"))  # the notice sits below the bar
    root.update()
    assert w.mute_bar.winfo_y() < w.notice.winfo_y() < w.view.winfo_y()
    w.mute_btn.invoke()
    root.update()
    assert not w.mute_bar.winfo_ismapped()

    w.bookmark_btn.invoke()
    w.bookmark_menu.invoke(1)
    assert ctl.calls[-2:] == [("bookmark", "bookmark", "button", {}), ("bookmark", "decision", "button", {})]
    assert "Decision added" in w.stat_lbl.cget("text")

    # a line's right-click action and the editor's save both go through the controller
    row = w.session.model.lines[1]
    w._bookmark_line(row, "question")
    assert ctl.calls[-1] == ("bookmark", "question", "line", {"folder": folder, "at_ms": 4000, "seq": row.seq})
    w.session.model.apply_marks([{"type": "mark", "id": 1, "kind": "bookmark", "at_ms": 100}])
    mark = w.session.model.marks[0]
    w._mark_edited(folder, mark, label="x", kind="action")
    w._delete_mark(mark)
    assert ctl.calls[-2:] == [("edit", 1, {"label": "x", "kind": "action"}), ("edit", 1, {"delete": True})]

    # not recording: no mute control, no Bookmark button
    state.phase = "ready"
    state.last_meeting = None
    ctl.folder = None
    w._refresh_all()
    root.update()
    assert not w.mute_btn.winfo_ismapped() and not w.bookmark_btn.winfo_ismapped()
    w.destroy()


def test_mark_editor_returns_only_what_changed(root):
    from huddle_helper.gui.dialogs import MarkEditor

    m = TranscriptModel()
    m.apply_marks([{"type": "mark", "id": 2, "kind": "decision", "at_ms": 1000}])
    row = m.marks[0]
    got = []
    ed = MarkEditor(root, m, row, got.append, lambda: got.append("deleted"))
    ed.label.set("Ship it")
    ed.kind.set("Question")
    ed._save()
    assert got == [{"label": "Ship it", "kind": "question"}]
    ed = MarkEditor(root, m, row, got.append, lambda: got.append("deleted"))
    ed._save()  # nothing edited
    assert got == [{"label": "Ship it", "kind": "question"}]
    ed = MarkEditor(root, m, row, got.append, lambda: got.append("deleted"))
    ed._delete()
    assert got[-1] == "deleted"


def test_settings_hotkeys_tab_shows_results_and_validates(root):
    from huddle_helper import config, hotkeys
    from huddle_helper.ui import SettingsDialog

    saved = []

    def results(bindings):
        return ({n: hotkeys.Registration(t, n != "decision",
                                         "already in use by another program" if n == "decision" else "")
                 for n, t in bindings.items() if t}, False)

    dlg = SettingsDialog(root, config.Settings(), {"inputs": [], "outputs": []}, saved.append, None,
                         hotkey_results=results)
    tab = dlg.hotkeys_tab
    assert set(tab.vars) == set(hotkeys.DEFAULTS)
    assert tab.status["bookmark"].cget("text").startswith("Free")
    assert "already in use by another program" in tab.status["decision"].cget("text")
    tab.vars["action"].set("Ctrl+Alt+B")  # the same as Bookmark
    assert "Same combination" in tab.status["action"].cget("text") and tab.validate()[0] is None
    tab.vars["action"].set("Nope")
    bindings, problem = tab.validate()
    assert bindings is None and problem.startswith("Action item:")
    tab.vars["action"].set("")  # off
    tab.vars["mute"].set("alt+ctrl+u")
    bindings, problem = tab.validate()
    assert problem == "" and bindings["mute"] == "Ctrl+Alt+U" and bindings["action"] == ""
    assert tab.status["action"].cget("text") == "Off"
    dlg._save()
    assert saved and saved[0].hotkeys["mute"] == "Ctrl+Alt+U" and saved[0].hotkeys["action"] == ""
