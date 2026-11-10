"""WP9c: mute reminders (L24), Start muted in the app/dialog, user-defined bookmark types (L28)."""

import gc
import json
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest

from huddle_helper import app, bookmark_types as bt, config, export, hotkeys, marks, mute_reminder
from huddle_helper.gui import logic
from huddle_helper.recorder import Components, Recorder
from huddle_helper.transcript_model import TranscriptModel
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence
from .helpers import make_meeting
from .test_mute import ManualCapture


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
    gc.collect()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# -- the reminder interval ---------------------------------------------------------------------------
def test_reminder_comes_every_interval_of_continuous_mute():
    c = Clock()
    r = mute_reminder.MuteReminder(600, c)
    assert r.poll(False) is None
    assert r.poll(True) is None  # the count starts here
    c.t += 599
    assert r.poll(True) is None
    c.t += 1
    assert r.poll(True) == 600
    c.t += 599
    assert r.poll(True) is None
    c.t += 1
    assert r.poll(True) == 1200
    assert r.muted_for_s() == 1200


def test_unmuting_starts_the_count_over_and_a_stall_gives_one_reminder():
    c = Clock()
    r = mute_reminder.MuteReminder(600, c)
    r.poll(True)
    c.t += 500
    assert r.poll(False) is None and r.muted_for_s() == 0
    r.poll(True)
    c.t += 599
    assert r.poll(True) is None  # not 1099 s of mute: the earlier stretch was ended by the unmute
    c.t += 1 + 1300  # polling stalled across two more intervals
    assert r.poll(True) == 1800  # one reminder, for the latest multiple
    c.t += 1
    assert r.poll(True) is None
    r.reset()
    assert r.poll(False) is None and r.muted_for_s() == 0


def test_reminder_wording():
    assert mute_reminder.duration_text(600) == "10 min" and mute_reminder.duration_text(1200) == "20 min"
    assert mute_reminder.duration_text(3) == "3 s" and mute_reminder.duration_text(60) == "1 min"
    assert mute_reminder.reminder_message(720) == "Your mic is still muted in the recording (12 min)."


def start_recorder(tmp_path, reminders, clock, interval=600, **kw):
    holder = {}

    def factory(**fkw):
        holder["cap"] = ManualCapture(fkw["on_audio"])
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer,
                       capture_factory=factory)
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "meetings")), comps,
                   on_mute_reminder=reminders.append, mute_reminder_s=interval)
    rec._reminder = mute_reminder.MuteReminder(interval, clock)
    folder = rec.start("Reminder", **kw)
    return rec, folder, holder["cap"]


def test_recorder_reminds_while_muted_resets_on_unmute_and_stops_with_stop(tmp_path):
    got, c = [], Clock()
    rec, folder, cap = start_recorder(tmp_path, got, c, start_muted=True)
    assert rec.status.mic_muted
    rec._publish()  # first sight of the mute
    c.t += 600
    rec._publish()
    assert got == [600] and rec.status.mute_reminder_s == 600 and rec.status.mute_reminder_at > 0
    rec._publish()
    assert got == [600]  # once per interval
    rec.set_mic_muted(False)
    assert rec.status.mute_reminder_s == 0  # the bar goes back to normal
    c.t += 5000
    rec._publish()
    assert got == [600]
    rec.set_mic_muted(True)
    rec._publish()
    c.t += 600
    rec._publish()
    assert got == [600, 600]
    rec.stop()
    c.t += 6000
    rec._publish()
    assert got == [600, 600] and rec.status.mute_reminder_s == 0


def test_mute_bar_says_how_long_after_a_reminder_and_is_stronger_for_a_minute():
    folder_less = TranscriptModel()
    rec = SimpleNamespace(mic_muted=True, mute_reminder_s=0.0, mute_reminder_at=0.0)
    state = SimpleNamespace(phase="recording", recording=rec, last_meeting="m1", finalizing="", finalize=None,
                            finalize_queue=0, message="")
    folder_less.meeting_id = "m1"
    plain = logic.mute_bar_view("m1", folder_less, state, now=10.0)
    assert plain is not None and "Muted for" not in plain.text and not plain.emphasis
    rec.mute_reminder_s, rec.mute_reminder_at = 1200.0, 100.0
    v = logic.mute_bar_view("m1", folder_less, state, now=130.0)
    assert v.text.startswith("Muted for 20 min") and v.emphasis
    assert not logic.mute_bar_view("m1", folder_less, state, now=161.0).emphasis
    rec.mic_muted = False
    assert logic.mute_bar_view("m1", folder_less, state, now=130.0) is None


# -- Start muted ---------------------------------------------------------------------------------------
def test_start_dialog_has_the_box_clear_each_time_and_passes_it(root):
    from huddle_helper.ui import StartDialog

    got = []
    d = StartDialog(root, lambda *a: got.append(a))
    assert d.start_muted.get() is False
    d.start_muted.set(True)
    d._start()
    assert got[-1] == ("meeting", [], [], True)
    d = StartDialog(root, lambda *a: got.append(a))
    assert d.start_muted.get() is False  # not remembered
    d.win.destroy()


def test_controller_start_muted_reaches_the_recorder(tmp_path, monkeypatch):
    seen = {}

    class FakeRecorder:
        def __init__(self, *a, **kw):
            seen["init"] = kw

        def start(self, title, terms=(), start_muted=False, attendees=()):
            seen["start_muted"] = start_muted
            return SimpleNamespace(meeting_id="m1")

    monkeypatch.setattr(app, "Recorder", FakeRecorder)
    monkeypatch.setenv(app.REMINDER_ENV, "7")
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)})
    monkeypatch.setattr(ctl, "_get_components", lambda: object())
    ctl.state.phase = "ready"
    ctl.start("t", start_muted=True)
    for _ in range(100):
        if ctl.state.phase == "recording":
            break
        import time
        time.sleep(0.02)
    assert seen["start_muted"] is True and seen["init"]["mute_reminder_s"] == 7.0
    assert callable(seen["init"]["on_mute_reminder"])
    monkeypatch.delenv(app.REMINDER_ENV)
    assert app._reminder_interval() == 600.0


def test_the_controller_notifies_through_the_normal_path():
    told = []
    ctl = app.AppController(notify=told.append)
    ctl._mute_reminder(1200)
    assert told == ["Your mic is still muted in the recording (20 min)."]


# -- bookmark types: validation ------------------------------------------------------------------------
def test_ids_come_from_names_and_avoid_reserved_and_taken():
    assert bt.slugify("Follow up with legal") == "follow-up-with-legal" and bt.slugify("リスク") == ""
    assert bt.make_id("Risk", []) == "risk" and bt.make_id("Risk", ["risk"]) == "risk-2"
    assert bt.make_id("Mute", []) == "mute-2"  # an action name is reserved
    assert bt.make_id("Decision!", []) == "decision-2" and bt.make_id("リスク", []) == "type"


def test_validate_accepts_up_to_four_and_keeps_existing_ids():
    rows = [("risk", "Risks", "ctrl+alt+r"), ("", "Follow up", ""), ("", "", ""), ("", "Idea", "Ctrl+Alt+I")]
    entries, problems = bt.validate(rows, hotkeys.DEFAULTS)
    assert problems == [] and entries == [
        {"id": "risk", "name": "Risks", "hotkey": "Ctrl+Alt+R"},  # renamed, id unchanged, hotkey canonical
        {"id": "follow-up", "name": "Follow up", "hotkey": ""},
        {"id": "idea", "name": "Idea", "hotkey": "Ctrl+Alt+I"}]
    too_many = [("", f"T{i}", "") for i in range(5)]
    assert bt.validate(too_many)[1][0].text == "At most 4 types."


@pytest.mark.parametrize("rows,field,fragment", [
    ([("", "", "Ctrl+Alt+R")], "name", "Give this type a name"),
    ([("", "x" * 25, "")], "name", "24 characters"),
    ([("", "decision", "")], "name", "built-in"),
    ([("", "Bookmarks", "")], "name", "built-in"),
    ([("", "Risk", ""), ("", "risk", "")], "name", "used twice"),
    ([("", "Risk", "Ctrl+Alt+B")], "hotkey", "already used by “Bookmark”"),
    ([("", "Risk", "Ctrl+Alt+R"), ("", "Idea", "ctrl+alt+r")], "hotkey", "already used by “Risk”"),
    ([("", "Risk", "R")], "hotkey", "modifier"),
    ([("", "Bad\x07name", "")], "name", "cannot be shown"),
])
def test_validate_rejects(rows, field, fragment):
    entries, problems = bt.validate(rows, hotkeys.DEFAULTS)
    assert entries is None and problems[-1].field == field and fragment in problems[-1].text


def test_hotkey_off_in_settings_does_not_block_a_type_using_it():
    entries, problems = bt.validate([("", "Risk", "Ctrl+Alt+B")], {**hotkeys.DEFAULTS, "bookmark": ""})
    assert problems == [] and entries[0]["hotkey"] == "Ctrl+Alt+B"


def test_the_altgr_warning_applies_to_a_type_hotkey():
    chk = hotkeys.check_combo("Ctrl+Alt+R", lambda combo: "®")
    assert chk.altgr == "®" and "swallow" in chk.warning


def test_settings_round_trip_drops_garbage_and_caps_at_four(tmp_path):
    s = config.Settings.from_dict({"custom_bookmark_types": [
        {"id": "risk", "name": "Risk", "hotkey": "Ctrl+Alt+R"}, {"id": "Bad Id", "name": "x"},
        {"id": "decision", "name": "Dup"}, {"id": "risk", "name": "Twice"}, "junk", {"id": "a", "name": "  "},
        *[{"id": f"t{i}", "name": f"T{i}"} for i in range(6)]]})
    assert [e["id"] for e in s.custom_bookmark_types] == ["risk", "t0", "t1", "t2"]
    path = tmp_path / "s.json"
    config.save(s, path)
    assert config.load(path).custom_bookmark_types == s.custom_bookmark_types
    assert config.Settings.from_dict({}).custom_bookmark_types == []
    assert config.Settings.from_dict({"custom_bookmark_types": "bad"}).custom_bookmark_types == []


# -- bookmark types: names in the meeting -------------------------------------------------------------
def mark(i, kind, at=1000):
    return {"type": "mark", "id": i, "kind": kind, "at_ms": at, "via": "hotkey"}


def test_custom_kinds_prefer_the_meetings_name_then_settings_in_settings_order():
    ms = marks.reduce([mark(1, "idea"), mark(2, "risk"), mark(3, "old-one"), mark(4, "decision")]).values()
    got = marks.custom_kinds(ms, {"risk": "Risk", "idea": "Idea!", "unused": "Unused"}, {"idea": "Idea"})
    assert list(got.items()) == [("risk", "Risk"), ("idea", "Idea"), ("old-one", "Old one")]


def test_a_removed_type_still_displays_and_exports_by_name(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 500, 900, "You", "We might slip.")],
                          marks=[mark(1, "decision", 600), mark(2, "risk", 1000), mark(3, "bookmark", 1100),
                                 mark(4, "idea", 1200)])
    folder.update_meta(mark_kinds={"idea": "Idea", "risk": "Risk"})  # Settings order: idea first
    m = TranscriptModel.load(folder)
    assert m.kind_name("risk") == "Risk"  # nothing in Settings at all: the name is in the meeting
    text = export.render(m, "md")
    heads = [ln for ln in text.splitlines() if ln.startswith("### ")]
    assert heads == ["### Decisions", "### Bookmarks", "### Idea", "### Risk"]
    assert "Risk 00:00:01" in text
    # an id the meeting does not name reads as a plain bookmark
    folder2 = make_meeting(tmp_path / "b", [], marks=[mark(1, "risk")])
    assert TranscriptModel.load(folder2).kind_name("risk") == "Bookmark"


def test_past_meeting_bookmarks_record_the_name_and_keep_it_after_removal(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 5000, 6000, "You", "hello.")])
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)})
    ctl.settings.custom_bookmark_types = [{"id": "risk", "name": "Risk", "hotkey": ""}]
    assert ctl.bookmark("risk", "line", folder=folder, at_ms=5000, seq=1)["action"] == "added"
    assert folder.read_meta()["mark_kinds"] == {"risk": "Risk"}
    ctl.settings.custom_bookmark_types = []  # removed from Settings
    ctl.edit_mark(folder, 1, label="Slip")
    assert folder.read_meta()["mark_kinds"] == {"risk": "Risk"}
    kinds = [e.get("kind") for e in read_events(folder.marks_path) if e["type"] == "mark"]
    assert kinds == ["risk"]  # marks.jsonl only holds the id; additive


def test_recorder_writes_the_name_at_once_and_a_later_removal_changes_nothing(tmp_path):
    got, c = [], Clock()
    rec, folder, cap = start_recorder(tmp_path, got, c)
    res = rec.bookmark("risk", "button", names={"risk": "Risk", "idea": "Idea"})
    assert res.action == "added"
    assert folder.read_meta()["mark_kinds"] == {"risk": "Risk"}  # before Stop, so a live window can name it
    rec.bookmark("decision", "line", at_ms=2000, names={})  # a line bookmark skips the 2 s rule
    cap.push(silence(0.3), silence(0.3))
    rec.stop()
    meta = folder.read_meta()
    assert meta["mark_kinds"] == {"risk": "Risk"} and meta["marks"] == 2  # Stop with Settings now empty
    assert TranscriptModel.load(folder).kind_name("risk") == "Risk"


def test_hotkeys_and_menu_items_include_the_custom_types():
    ctl = app.AppController()
    ctl.settings.custom_bookmark_types = [{"id": "risk", "name": "Risk", "hotkey": "Ctrl+Alt+R"},
                                          {"id": "idea", "name": "Idea", "hotkey": ""}]
    assert ctl.hotkey_bindings() == {**hotkeys.DEFAULTS, "risk": "Ctrl+Alt+R"}
    items = logic.bookmark_items(ctl.hotkey_bindings(), ctl.custom_names())
    assert [i.menu_text for i in items][-2:] == ["◆ Risk    Ctrl+Alt+R", "◆ Idea"]
    assert [i.kind for i in items] == ["bookmark", "decision", "action", "question", "risk", "idea"]
    assert ctl.hotkey_pressed("risk")["action"] == "none"  # not recording


def test_hotkey_thread_receives_custom_bindings_as_actions():
    # registration needs Windows; the seam is the bindings dict, parsed like any other
    combos = {n: hotkeys.parse(t).text for n, t in {"risk": "ctrl+alt+shift+r"}.items()}
    assert combos == {"risk": "Ctrl+Alt+Shift+R"}


# -- Settings tab -----------------------------------------------------------------------------------------
def test_settings_dialog_saves_types_and_blocks_a_clash(root, monkeypatch):
    from huddle_helper import ui

    saved = []
    shown = []
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *a, **k: shown.append(a[1]))
    s = config.Settings(custom_bookmark_types=[{"id": "risk", "name": "Risk", "hotkey": ""}])
    d = ui.SettingsDialog(root, s, {"inputs": [], "outputs": []}, saved.append)
    t = d.types_tab
    assert t.names[0].get() == "Risk"
    t.names[1].set("Idea")
    t.keys[1].set("Ctrl+Alt+B")  # the Bookmark hotkey
    d._save()
    assert saved == [] and "already used by" in shown[-1]
    t.keys[1].set("Ctrl+Alt+I")
    t.names[0].set("")  # remove Risk
    d._save()
    assert saved[0].custom_bookmark_types == [{"id": "idea", "name": "Idea", "hotkey": "Ctrl+Alt+I"}]


def test_settings_dialog_leaves_types_alone_when_unchanged(root):
    from huddle_helper import ui

    saved = []
    s = config.Settings(custom_bookmark_types=[{"id": "risk", "name": "Risk", "hotkey": "Ctrl+Alt+R"}])
    d = ui.SettingsDialog(root, s, {"inputs": [], "outputs": []}, saved.append)
    d._save()
    assert saved[0].custom_bookmark_types == s.custom_bookmark_types
