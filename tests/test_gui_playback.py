"""The window's click-to-hear controls (WP7), against a stub controller: no sound, no device."""

from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import meeting as mt, playback
from huddle_helper.app import AppState, PlayResult
from huddle_helper.gui import logic
from huddle_helper.recorder import Status
from huddle_helper.transcript_model import TranscriptModel

from .conftest import tone
from .helpers import make_meeting, play_stub
from .test_gui_smoke import FakeApp, root  # noqa: F401 - root is a fixture

LINES = [("mic", 1000, 4500, "You", "first line here."),
         ("system", 5000, 9000, "SPEAKER_01", "second line, from the call."),
         ("system", 10_000, 12_000, "SPEAKER_02", "third.")]


def with_audio(folder, seconds=15.0):
    for src in ("mic", "system"):
        sf.write(str(folder.audio_file(src)), tone(300, seconds), 16_000, format="FLAC", subtype="PCM_16")


def glyph_at(view, i):
    return view.text.get(view._start(i), f"{view._start(i)}+1c")


def click_glyph(view, i):
    """A click on the play control of row ``i``."""
    view.text.update_idletasks()
    box = view.text.bbox(view._start(i))
    assert box, "row not visible"
    view._click(SimpleNamespace(x=box[0] + 2, y=box[1] + box[3] // 2))
    view.update()


def click_text(view, i):
    box = view.text.bbox(f"{view._start(i)}+12c")
    view._click(SimpleNamespace(x=box[0] + 2, y=box[1] + box[3] // 2))


def make_window(root, tmp_path, folder, state=None, app_cls=FakeApp):
    from huddle_helper.gui.window import MainWindow

    ctl = app_cls(tmp_path, state or AppState(phase="ready"), None)
    w = MainWindow(root, ctl)
    w.show(folder)
    root.update()
    return w, ctl


def test_every_line_has_a_play_control_that_is_on_when_audio_is_kept(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    with_audio(folder)
    w, ctl = make_window(root, tmp_path, folder)
    assert [glyph_at(w.view, i) for i in range(3)] == ["▶", "▶", "▶"]
    assert len(w.view.text.tag_ranges("play-on")) == 6 and not w.view.text.tag_ranges("play-off")
    click_glyph(w.view, 1)
    assert ctl.play_calls == [("play", "line:system:5000", False)]
    w.destroy()


def test_clicking_the_playing_control_stops_it_and_the_row_shows_a_stop_square(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    with_audio(folder)
    w, ctl = make_window(root, tmp_path, folder)
    ctl.playing_key = "line:system:5000"
    w._tick_fast()
    assert [glyph_at(w.view, i) for i in range(3)] == ["▶", "■", "▶"]
    assert w.view.text.tag_ranges("playing")  # the row's background
    click_glyph(w.view, 1)
    assert ctl.play_calls[-1] == ("stop",)  # not a second play
    ctl.playing_key = None
    w._tick_fast()
    assert [glyph_at(w.view, i) for i in range(3)] == ["▶", "▶", "▶"] and not w.view.text.tag_ranges("playing")
    assert w.view.row_count == 3 and "second line, from the call." in w.view.plain_text()  # text untouched
    w.destroy()


def test_space_plays_the_selected_line_and_esc_stops(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    with_audio(folder)
    w, ctl = make_window(root, tmp_path, folder)
    assert w.view._space(None) == "break" and ctl.play_calls == []  # nothing selected yet
    click_text(w.view, 2)
    assert w.view.selected_row.text == "third." and w.view.text.tag_ranges("selrow")
    w.view._space(None)
    assert ctl.play_calls == [("play", "line:system:10000", False)]
    w.win.deiconify()
    w.win.update()
    w.win.event_generate("<Escape>")
    w.win.update()
    assert ctl.play_calls[-1] == ("stop",)
    w.destroy()


def test_a_bookmark_row_plays_from_10_seconds_before_on_both_streams(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING,
                          marks=[{"type": "mark", "id": 3, "kind": "decision", "at_ms": 11_000}])
    with_audio(folder)
    w, ctl = make_window(root, tmp_path, folder)
    clips = []
    ctl.play_clip = lambda f, clip, confirmed=False: clips.append(clip) or PlayResult("playing")
    mark_index = next(i for i, r in enumerate(w.view._rows) if r.__class__.__name__ == "MarkRow")
    click_glyph(w.view, mark_index)
    clip = clips[0]
    assert (clip.key, clip.start_ms, clip.end_ms, clip.sources) == ("mark:3", 1_000, 16_000, ("mic", "system"))
    menu = w.view.build_menu(w.view._rows[mark_index])
    assert menu.entrycget(0, "label") == "Play from 10 s before"
    w.destroy()


def test_controls_are_greyed_out_with_the_reason_when_the_audio_was_deleted(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, final=LINES)  # DONE, no audio folder content
    w, ctl = make_window(root, tmp_path, folder)
    assert len(w.view.text.tag_ranges("play-off")) == 6 and not w.view.text.tag_ranges("play-on")
    click_glyph(w.view, 0)
    assert ctl.play_calls == []  # nothing is started
    assert w.notice.cget("text") == playback.DELETED_AFTER_FINAL and "Turn on Keep audio" in w.notice.cget("text")
    assert w.notice.cget("bg") == "#fef3c7"
    menu = w.view.build_menu(w.view._rows[0])
    assert menu.entrycget(0, "label") == "Play" and str(menu.entrycget(0, "state")) == "disabled"
    w.destroy()


def test_while_recording_older_lines_say_available_after_stop(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING)
    state = AppState(phase="recording", last_meeting=folder.meeting_id)
    state.recording = Status(state="recording", meeting_id=folder.meeting_id, elapsed_s=700)
    ctl_oldest = {"mic": 6_000, "system": 6_000}  # the ring starts at 6 s: lines 1 (mic, 1 s) is out, line 3 is in
    w, ctl = make_window(root, tmp_path, folder, state)
    ctl.recent_oldest_ms = lambda f: ctl_oldest
    ctl.folder = folder
    w._refresh_all()
    root.update()
    states = [logic.row_play_state(w.audio_context(), r) for r in w.session.model.lines]
    assert [s.enabled for s in states] == [False, False, True]
    assert states[0].reason == playback.AFTER_STOP and "Available after Stop" in states[0].reason
    assert len(w.view.text.tag_ranges("play-off")) == 4  # two glyphs, and the tag spans two ranges (tab apart)
    w.destroy()


def test_keep_this_meetings_audio_box_only_while_recording_or_processing(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    folder.update_meta(settings={"keep_audio": False, "echo_dedupe": True})
    w, ctl = make_window(root, tmp_path, folder)
    st = w.app.state
    st.finalize = SimpleNamespace(meeting_id=folder.meeting_id, stage="Grouping voices", frac=0.2, overall=0.3,
                                  eta_s=None)
    w._refresh_all()
    root.update()
    assert w.keep_chk.winfo_ismapped() and w.keep_var.get() is False
    w.keep_chk.invoke()
    assert ctl.play_calls[-1] == ("keep", True)
    done = make_meeting(tmp_path, LINES, final=LINES, title="Done")
    w.open_meeting(done)
    st.finalize = None
    w._refresh_all()
    root.update()
    assert not w.keep_chk.winfo_ismapped()
    w.destroy()


def test_keep_audio_view_shows_the_meetings_own_setting():
    m = TranscriptModel()
    m.set_meta({"status": "recording", "settings": {"keep_audio": True}})
    state = AppState(phase="recording", last_meeting="x")
    v = logic.keep_audio_view("x", m, state)
    assert v.visible and v.checked and v.text == "Keep this meeting's audio"
    m.set_meta({"status": "recording", "settings": {}})
    assert not logic.keep_audio_view("x", m, state).checked
    m2 = TranscriptModel()
    m2.set_meta({"status": "complete"})
    m2.final = True
    assert not logic.keep_audio_view("y", m2, AppState()).visible


def test_speaker_chips_and_hear_this_voice(root, tmp_path):
    lines = [("system", 1000, 3000, "SPEAKER_01", "short."),
             ("system", 5000, 10_000, "SPEAKER_01", "a five second line of speech."),
             ("system", 12_000, 14_000, "SPEAKER_02", "another voice."),
             ("mic", 20_000, 22_000, "You", "me.")]
    folder = make_meeting(tmp_path, lines, status=mt.STATUS_PROCESSING)
    with_audio(folder, 30.0)
    w, ctl = make_window(root, tmp_path, folder)
    assert w.speakers.winfo_ismapped()
    chips = w.speakers.get("1.0", "end-1c")
    assert chips.count("●") == 3 and "SPEAKER_01" in chips and "SPEAKER_02" in chips and "You" in chips
    clips = []
    ctl.play_clip = lambda f, clip, confirmed=False: clips.append(clip) or PlayResult("playing")
    w._hear_voice("SPEAKER_01")
    assert clips[-1].key == "line:system:5000"  # the 5 s line, not the 2 s one
    w.chip_menu.tk_popup = lambda *a: None  # a real popup would run its own loop until dismissed
    w._chip_menu("SPEAKER_02", 10, 10)
    assert w.chip_menu.entrycget(0, "label") == "Hear this voice" and str(w.chip_menu.entrycget(0, "state")) == "normal"
    row_menu = w.view.build_menu(w.view._rows[0])
    labels = [row_menu.entrycget(i, "label") for i in range(row_menu.index("end") + 1) if row_menu.type(i) == "command"]
    assert labels[:2] == ["Play", "Hear this voice (SPEAKER_01)"]
    w.destroy()


def test_hear_this_voice_is_disabled_with_a_reason_without_audio(root, tmp_path):
    folder = make_meeting(tmp_path, LINES, final=LINES)
    w, ctl = make_window(root, tmp_path, folder)
    ok, why = w._voice_state("SPEAKER_01")
    assert not ok and why == playback.DELETED_AFTER_FINAL
    w._hear_voice("SPEAKER_01")
    assert ctl.play_calls == [] and w.notice.cget("text") == playback.DELETED_AFTER_FINAL
    w.chip_menu.tk_popup = lambda *a: None
    w._chip_menu("SPEAKER_01", 10, 10)
    assert str(w.chip_menu.entrycget(0, "state")) == "disabled" and playback.DELETED_AFTER_FINAL[:30] in w.chip_menu.entrycget(1, "label")
    w.destroy()


def test_headphone_notice_shows_in_the_notice_bar_and_the_confirmation_asks_first(root, tmp_path, monkeypatch):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    with_audio(folder)
    w, ctl = make_window(root, tmp_path, folder)
    ctl.play_clip = lambda f, clip, confirmed=False: ctl.play_calls.append(("play", clip.key, confirmed)) or PlayResult(
        "playing", notice=playback.HEADPHONES)
    click_glyph(w.view, 0)
    assert w.notice.cget("text") == playback.HEADPHONES and "people on the call will hear" in w.notice.cget("text")

    asked = []
    answers = iter([False, True])
    monkeypatch.setattr("huddle_helper.gui.window.messagebox.askyesno",
                        lambda title, msg, **kw: asked.append(msg) or next(answers))
    ctl.play_calls.clear()
    ctl.play_clip = lambda f, clip, confirmed=False: (
        ctl.play_calls.append(("play", clip.key, confirmed))
        or (PlayResult("playing") if confirmed else PlayResult("confirm", playback.HEADPHONES_CONFIRM)))
    click_glyph(w.view, 1)  # the user says no
    assert ctl.play_calls == [("play", "line:system:5000", False)] and "Play it anyway?" in asked[0]
    ctl.play_calls.clear()
    click_glyph(w.view, 1)  # yes: asked again, then played with confirmed=True
    assert ctl.play_calls == [("play", "line:system:5000", False), ("play", "line:system:5000", True)]
    w.destroy()


def test_copying_selected_text_leaves_out_the_play_controls():
    assert logic.copyable("▶\t09:30:01\tYou\thello.\n■\t09:30:05\tDana\tworld.") == "09:30:01\tYou\thello.\n09:30:05\tDana\tworld."
    assert logic.copyable("just words") == "just words" and logic.copyable("▶ not a control") == "▶ not a control"
