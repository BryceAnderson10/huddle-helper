"""Attendees in the window (WP9a): the numbered strip, number keys with their guards, the confirmation and its
Undo, the "Who is this?" bar for new voices, and the picker lists. Names are invented."""

import tkinter as tk
from types import SimpleNamespace

from huddle_helper import meeting as mt, tags
from huddle_helper.app import AppState
from huddle_helper.recorder import Status
from huddle_helper.writer import read_events

from .helpers import make_meeting
from .test_gui_naming import NamingApp, sub_labels
from .test_gui_playback import make_window
from .test_gui_smoke import root  # noqa: F401 - a fixture

LIVE = [("mic", 500, 1500, "You", "first line here."),
        ("system", 5000, 9000, "SPEAKER_01", "second line, from the call."),
        ("system", 10_000, 12_000, "SPEAKER_02", "third one.")]
CREW = ["Ana", "Ben", "Cy"]


def live_window(root, tmp_path, attendees=CREW, lines=LIVE, status=mt.STATUS_RECORDING):
    folder = make_meeting(tmp_path, lines, status=status)
    if attendees is not None:
        folder.update_meta(attendees=attendees)
    st = AppState(phase="recording", last_meeting=folder.meeting_id)
    st.recording = Status(state="recording", meeting_id=folder.meeting_id, elapsed_s=30, segments=len(lines))
    w, ctl = make_window(root, tmp_path, folder, state=st, app_cls=NamingApp)
    ctl.folder = folder
    return w, ctl, folder


def key(char, state=0):
    return SimpleNamespace(char=char, state=state)


def tag_events(folder):
    return read_events(folder.tags_path)


def bar_buttons(w):
    return [b.cget("text") for b in w.prompt_btns.winfo_children()]


def test_strip_numbers_the_attendees_and_names_the_target(root, tmp_path):
    w, _, _ = live_window(root, tmp_path)
    root.update()
    assert w.strip.winfo_ismapped() and w.strip.cget("text") == "Keys 1–3 name SPEAKER_01:  1 Ana   2 Ben   3 Cy"
    w.destroy()


def test_no_attendees_means_no_strip_and_the_keys_do_nothing(root, tmp_path):
    w, ctl, folder = live_window(root, tmp_path, attendees=None)
    assert not w._strip_shown and w._on_key(key("1")) is None
    assert not folder.tags_path.exists()
    w.destroy()


def test_a_number_key_answers_the_who_is_this_bar_and_offers_undo(root, tmp_path):
    w, ctl, folder = live_window(root, tmp_path)
    assert w._on_key(key("2")) == "break"  # the bar asks about SPEAKER_01: that is the voice the key names
    root.update()
    ev = tag_events(folder)
    assert [(e["type"], e["label"], e["name"]) for e in ev] == [("tag", "SPEAKER_01", "Ben")]
    assert w.session.model.lines[1].name == "Ben" and w.session.model.lines[2].name is None
    assert w.prompt_text.cget("text") == "SPEAKER_01 is now Ben ·"
    assert bar_buttons(w) == ["Undo", "OK"]
    # undo puts the label back to unnamed
    w._prompt_action("undo")
    root.update()
    assert tag_events(folder)[-1]["type"] == "untag" and w.session.model.lines[1].name is None
    assert w.prompt_text.cget("text").startswith("New voice SPEAKER_02")  # on to the next voice (an undone one is not asked twice)
    w.destroy()


def test_without_a_bar_the_key_names_the_latest_unnamed_call_voice(root, tmp_path):
    w, ctl, folder = live_window(root, tmp_path)
    w._prompt_action("dismiss")  # Not now on SPEAKER_01, then on SPEAKER_02
    w._prompt_action("dismiss")
    root.update()
    assert not w.prompt_bar.winfo_ismapped()
    assert w.strip.cget("text").startswith("Keys 1–3 name SPEAKER_02:")  # the latest call line's unnamed speaker
    w._on_key(key("2"))
    root.update()
    assert [(e["label"], e["name"]) for e in tag_events(folder)] == [("SPEAKER_02", "Ben")]
    assert w.prompt_text.cget("text") == "SPEAKER_02 is now Ben ·"
    w.destroy()


def test_keys_rename_again_and_undo_restores_the_earlier_name(root, tmp_path):
    w, _, folder = live_window(root, tmp_path, lines=LIVE[:2])
    w._on_key(key("1"))  # SPEAKER_01 -> Ana (the only call voice)
    w._on_key(key("3"))  # nothing unnamed is left: the latest call line is renamed
    assert w.session.model.lines[-1].name == "Cy" and w.prompt_text.cget("text") == "Ana is now Cy ·"
    w._prompt_action("undo")
    assert w.session.model.lines[-1].name == "Ana"
    w.destroy()


def test_keys_are_ignored_in_text_boxes_the_final_view_and_other_pages(root, tmp_path, monkeypatch):
    w, _, folder = live_window(root, tmp_path)
    entry = tk.Entry(w.win)
    monkeypatch.setattr(w.win, "focus_get", lambda: entry)
    assert w._on_key(key("1")) is None
    text = tk.Text(w.win)
    monkeypatch.setattr(w.win, "focus_get", lambda: text)
    assert w._on_key(key("1")) is None
    monkeypatch.setattr(w.win, "focus_get", lambda: w.view.text)  # the read-only transcript does not block
    assert w._on_key(key("1")) == "break"
    assert w._on_key(key("1", 0x4)) is None and w._on_key(key("0")) is None and w._on_key(key("a")) is None  # Ctrl+1, 0, a
    w.page = "meetings"
    assert w._on_key(key("2")) is None
    w.page = "transcript"
    assert len(tag_events(folder)) == 1  # only the one that was meant
    w.destroy()

    final = make_meeting(tmp_path / "f", LIVE, final=LIVE)
    final.update_meta(attendees=CREW)
    w2, _ = make_window(root, tmp_path, final, app_cls=NamingApp)
    assert w2.session.model.final and w2._on_key(key("1")) is None and not w2._strip_shown
    assert not final.tags_path.exists()
    w2.destroy()


def test_a_key_beyond_the_list_says_so_and_names_nobody(root, tmp_path):
    w, _, folder = live_window(root, tmp_path)
    assert w._on_key(key("7")) == "break"
    assert w._feedback[0] == "No attendee 7 (the list has 3)" and not folder.tags_path.exists()
    w.destroy()


def test_a_new_voice_raises_the_bar_without_a_dialog_or_a_focus_change(root, tmp_path):
    w, _, folder = live_window(root, tmp_path)
    root.update()
    assert w.prompt_bar.winfo_ismapped()
    assert w.prompt_text.cget("text") == "New voice SPEAKER_01 (“second line, from the call.”). Who is this?"
    assert bar_buttons(w) == ["1 Ana", "2 Ben", "3 Cy", "Other…", "Not now"]
    assert not [c for c in w.win.winfo_children() if isinstance(c, tk.Toplevel)]  # nothing modal
    assert all(str(b.cget("takefocus")) in ("0", "False", "") for b in w.prompt_btns.winfo_children())
    # one at a time: SPEAKER_02 waits behind SPEAKER_01
    assert [v.label for v in w.prompter.queue] == ["SPEAKER_02"]
    w._prompt_action("pick:3")
    root.update()
    assert [(e["label"], e["name"]) for e in tag_events(folder)] == [("SPEAKER_01", "Cy")]
    assert w.prompt_text.cget("text") == "SPEAKER_01 is now Cy ·"
    w._prompt_action("dismiss")  # the confirmation goes; the next voice is asked
    root.update()
    assert w.prompt_text.cget("text").startswith("New voice SPEAKER_02") and bar_buttons(w)[:2] == ["1 Ana", "2 Ben"]
    w._prompt_action("dismiss")  # Not now
    root.update()
    assert not w.prompt_bar.winfo_ismapped()
    w.destroy()


def test_a_later_new_voice_is_asked_when_it_first_speaks(root, tmp_path):
    w, _, folder = live_window(root, tmp_path, lines=LIVE[:2])
    w._prompt_action("dismiss")
    root.update()
    assert not w.prompt_bar.winfo_ismapped()
    mt.append_jsonl(folder.live_path, {"type": "segment", "seq": 9, "start_ms": 20_000, "end_ms": 21_000,
                                       "source": "system", "speaker": "SPEAKER_04", "text": "Can you hear me?",
                                       "confidence": 0.9})
    w._tick_fast()
    w._refresh_all()
    root.update()
    assert w.prompt_bar.winfo_ismapped() and "SPEAKER_04" in w.prompt_text.cget("text")
    w.destroy()


def test_the_prompt_goes_away_when_the_voice_is_named_by_a_chip_or_key(root, tmp_path):
    w, _, folder = live_window(root, tmp_path, lines=LIVE[:2])
    w._apply_person_name("SPEAKER_01", "Ana")  # the chip's own path
    w._refresh_all()
    root.update()
    assert not w.prompt_bar.winfo_ismapped()
    w.destroy()


def test_names_picker_and_line_menu_offer_unused_attendees_first(root, tmp_path):
    w, _, folder = live_window(root, tmp_path)
    w._apply_person_name("SPEAKER_01", "Ben")
    row = w.session.model.lines[-1]  # SPEAKER_02, unnamed
    nm = w._line_menu(row)
    assert nm.enabled and nm.choices == ("Ana", "Cy", "Ben")
    menu = w.view.build_menu(row)
    idx = next(i for i in range(menu.index("end") + 1) if menu.type(i) == "cascade" and
               menu.entrycget(i, "label") == "This line is…")
    assert sub_labels(menu.nametowidget(menu.entrycget(idx, "menu")))[:4] == ["Ana", "Cy", "Ben", "Someone else…"]
    w.name_person("SPEAKER_02")
    picker = [c for c in w.win.winfo_children() if isinstance(c, tk.Toplevel)][-1]
    lists = [c for c in picker.winfo_children()[0].winfo_children() if c.winfo_class() == "Listbox"]
    assert list(lists[0].get(0, "end")) == ["Ana", "Cy", "Ben"]
    labels = [c.cget("text") for c in picker.winfo_children()[0].winfo_children() if c.winfo_class() == "TLabel"]
    assert "On the call, and used in this meeting" in labels
    picker.destroy()
    w.destroy()


def test_start_dialog_passes_names_and_adds_them_to_the_terms_only_when_ticked(root):
    from huddle_helper.ui import StartDialog

    got = []
    d = StartDialog(root, lambda *a: got.append(a))
    d.title.set("Weekly")
    d.attendees.insert("1.0", "Ana, Ben\nCy")
    d.terms.text.insert("1.0", "QDM")
    d._start()
    assert got == [("Weekly", ["QDM"], ["Ana", "Ben", "Cy"], False)]  # names are not terms by default

    d = StartDialog(root, lambda *a: got.append(a))
    d.attendees.insert("1.0", "Ana; ben")
    d.names_as_terms.set(True)
    d._start()
    assert got[-1] == ("meeting", ["Ana", "ben"], ["Ana", "ben"], False)

    d = StartDialog(root, lambda *a: got.append(a))
    d._start()
    assert got[-1] == ("meeting", [], [], False)
