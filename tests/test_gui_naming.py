"""Naming speakers in the window (WP8): chips, the picker, "This line is…", merged chips, the notice
bar after the swap, naming in the final view. Names are invented."""

import json

from huddle_helper import meeting as mt, tags
from huddle_helper.app import AppController, AppState
from huddle_helper.gui import logic
from huddle_helper.transcript_model import TranscriptModel

from .helpers import make_meeting
from .test_gui_playback import make_window
from .test_gui_smoke import FakeApp, root  # noqa: F401 - root is a fixture

LIVE = [("mic", 500, 1500, "You", "first line here."),
        ("system", 5000, 9000, "SPEAKER_01", "second line, from the call."),
        ("system", 10_000, 12_000, "SPEAKER_02", "third one."),
        ("system", 14_000, 16_000, "SPEAKER_03", "fourth one."),
        ("system", 18_000, 19_000, "SPEAKER_01", "fifth one.")]


class NamingApp(FakeApp):
    """The real controller's naming methods over a stub (they only write files and ping ``_on_change``)."""
    tag_speaker = AppController.tag_speaker
    tag_line = AppController.tag_line
    set_final_name = AppController.set_final_name

    def _on_change(self):
        pass


def chips(w):
    return w.speakers.get("1.0", "end-1c")


def sub_labels(menu):
    return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) in ("command", "cascade")]


def test_chips_ask_who_is_this_for_unnamed_voices_but_not_for_you(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, status=mt.STATUS_RECORDING)
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    text = chips(w)
    assert "SPEAKER_01 · Who is this?" in text and "SPEAKER_03 · Who is this?" in text
    assert "You ·" not in text and "You" in text
    assert w.speakers.tag_cget("chip1", "background") == "#fef3c7"  # the "new voice" colour
    w.destroy()


def test_naming_a_chip_names_past_and_future_lines_and_merged_chips_share_a_name(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, status=mt.STATUS_RECORDING)
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    w._apply_person_name("SPEAKER_01", "Ana")
    w._apply_person_name("SPEAKER_02", "Ana")  # the live pass split one person into two labels
    root.update()
    assert tags.reduce(tags_events(folder)).speakers == {"SPEAKER_01": "Ana", "SPEAKER_02": "Ana"}
    text = chips(w)
    assert text.count("Ana") == 1 and "SPEAKER_01" not in text  # one chip
    assert [c.display for c in logic.speaker_chips(w.session.model)] == ["You", "Ana", "SPEAKER_03"]
    # a line that arrives later under the same label is named at once
    mt.append_jsonl(folder.live_path, {"type": "segment", "seq": 99, "start_ms": 30_000, "end_ms": 31_000,
                                       "source": "system", "speaker": "SPEAKER_01", "text": "later.", "confidence": 0.9})
    w._tick_fast()
    assert w.session.model.lines[-1].name == "Ana"
    w.destroy()


def tags_events(folder):
    from huddle_helper.writer import read_events
    return read_events(folder.tags_path)


def test_this_line_is_names_one_line_and_chip_naming_still_reaches_it(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, status=mt.STATUS_RECORDING)
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    w._apply_person_name("SPEAKER_01", "Ana")
    row = next(r for r in w.session.model.lines if r.start_ms == 18_000)
    w._name_line(row, "Ben")
    root.update()
    m = w.session.model
    assert [r.name for r in m.lines if r.label == "SPEAKER_01"] == ["Ana", "Ben"]
    assert "Ben" in chips(w) and "Ana" in chips(w)
    # the menu offers the names in use, the "someone else" entry, and a way back
    row = next(r for r in m.lines if r.start_ms == 18_000)
    menu = w.view.build_menu(row)
    sub = menu.nametowidget(menu.entrycget(next(i for i in range(menu.index("end") + 1)
                                                if menu.type(i) == "cascade" and menu.entrycget(i, "label") == "This line is…"), "menu"))
    assert sub_labels(sub) == ["Ana", "Someone else…", "Back to the speaker's name"]
    w._name_line(row, "")
    w._tick_fast()
    assert [r.name for r in w.session.model.lines if r.label == "SPEAKER_01"] == ["Ana", "Ana"]
    # renaming the Ben chip while a line carries its own name goes to the line, not the whole speaker
    w._name_line(row, "Ben")
    plan = logic.name_plan(w.session.model, "Ben")
    assert plan.labels == () and [r.start_ms for r in plan.lines] == [18_000]
    w.destroy()


def test_you_can_be_named_and_remove_name_goes_back_to_the_label(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, status=mt.STATUS_RECORDING)
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    w._apply_person_name("You", "Cy")
    assert tags.reduce(tags_events(folder)).speakers == {"You": "Cy"}
    w._apply_person_name("Cy", None)
    w._tick_fast()
    assert not tags.reduce(tags_events(folder)).speakers
    assert w.session.model.lines[0].name is None
    w.destroy()


def test_this_line_is_is_disabled_in_the_final_view(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, final=LIVE)
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    menu = w.view.build_menu(w.session.model.lines[1])
    entry = next(i for i in range(menu.index("end") + 1) if menu.type(i) == "command" and menu.entrycget(i, "label") == "This line is…")
    assert str(menu.entrycget(entry, "state")) == "disabled"
    w.destroy()


def test_naming_in_the_final_view_writes_speakers_json_and_keeps_other_names(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, final=LIVE)
    mt.write_json_atomic(folder.speakers_path, {"speakers": {"SPEAKER_03": "Dan"}, "updated_at": "x"})
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    w._apply_person_name("SPEAKER_01", "Ana")
    data = json.loads(folder.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"] == {"SPEAKER_03": "Dan", "SPEAKER_01": "Ana"}
    assert not folder.tags_path.exists()  # tags are for the live file only
    assert "Ana" in chips(w) and "Dan" in chips(w)
    w.destroy()


def test_conflict_and_unnamed_speakers_show_in_the_notice_bar_after_the_swap(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, final=LIVE)
    mt.write_json_atomic(folder.speakers_path, {
        "speakers": {"SPEAKER_01": "Ana"}, "from_tags": True, "updated_at": "x",
        "conflicts": [{"speaker": "SPEAKER_01", "names": {"Ana": 41_000, "Ben": 12_000}}]})
    w, _ = make_window(root, tmp_path, folder, app_cls=NamingApp)
    text = w.notice.cget("text")
    assert "put Ana and Ben together (SPEAKER_01)" in text and "41 s of Ana, 12 s of Ben" in text
    assert "Not named yet: SPEAKER_02, SPEAKER_03" in text
    # settling the conflict in the final view removes it from the bar
    w._apply_person_name("Ana", "Ben")
    w._refresh_all()
    assert "together" not in w.notice.cget("text")
    w.destroy()


def test_no_names_at_all_means_no_unnamed_notice(root, tmp_path):
    folder = make_meeting(tmp_path, LIVE, final=LIVE)
    model = TranscriptModel.load(folder)
    assert logic.naming_notices(model) == []
    assert logic.notice_view(folder.meeting_id, model, AppState()) is None


def test_name_choices_exclude_the_current_name_and_picker_accepts_enter(root, tmp_path):
    from huddle_helper.gui.dialogs import NamePicker

    got = []
    p = NamePicker(root, "Who is this?", "Who?", ["Ana", "Ben"], on_done=got.append)
    p.list.selection_set(1)
    p._pick()
    assert p.name.get() == "Ben"
    p.entry.delete(0, "end")
    p.accept()  # empty: nothing happens, the dialog stays
    assert got == [] and p.win.winfo_exists()
    p.entry.insert(0, "  Cy ")
    p.accept()
    assert got == ["Cy"]
