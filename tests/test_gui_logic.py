"""The Transcript window's texts and decisions (gui/logic.py), the replay driver and the new setting."""

from datetime import datetime, timezone

import pytest

from huddle_helper import app, config, meeting as mt
from huddle_helper.app import AppState, FinalizeProgress
from huddle_helper.gui import logic, theme
from huddle_helper.recorder import Status
from huddle_helper.replay import Replay, check_output_dir, next_step, replay_files
from huddle_helper.transcript_model import GapRow, Insert, MarkRow, Remove, Reset, TranscriptModel, Update

from .helpers import make_meeting


def model_for(tmp_path, **kw):
    return TranscriptModel.load(make_meeting(tmp_path, [("system", 0, 2000, "SPEAKER_00", "hello there."),
                                                        ("mic", 3000, 4000, "You", "hi.")], **kw))


# -- ops ----------------------------------------------------------------------
def test_normalize_ops_redraws_when_there_is_a_reset():
    row = MarkRow(1, "bookmark", "", 0)
    assert logic.normalize_ops([Insert(0, row), Remove(0)]) == [Insert(0, row), Remove(0)]
    assert logic.normalize_ops([Insert(0, row), Reset(), Insert(1, row)]) is None
    assert logic.normalize_ops([]) == []


# -- row texts -----------------------------------------------------------------
def test_line_time_is_clock_time_from_the_meeting_start(tmp_path):
    m = model_for(tmp_path)  # fixture started 09:30:00 UTC
    assert logic.line_time(m, 0) == "09:30:00"
    assert logic.line_time(m, 61_500) == "09:31:01"
    assert logic.line_time(TranscriptModel(), 65_000) == "01:05"  # no start known: elapsed


def test_short_name_and_gap_and_mark_texts(tmp_path):
    assert logic.short_name("Christopher Montgomery-Smythe", 12) == "Christopher…"
    assert logic.short_name("Dana") == "Dana"
    m = model_for(tmp_path)
    muted = GapRow("mic", 60_000, 120_000, "muted")
    assert logic.gap_text(muted, m) == "— You muted your mic 09:31:00–09:32:00 (1 min) —"
    dev = GapRow("system", 5000, 9000, "device changed")
    assert logic.gap_text(dev, m) == ("— No audio from the call 09:30:05–09:30:09 (4 s); "
                                      "device changed —")
    assert logic.mark_text(MarkRow(1, "decision", "Ship Friday", 0), m) == "Decision · Ship Friday"
    assert logic.mark_text(MarkRow(2, "bookmark", "", 0), m) == "Bookmark"


# -- phases, header, notice, status --------------------------------------------------
def recording_state(mid, **kw):
    st = AppState(phase="recording", last_meeting=mid)
    st.recording = Status(state="recording", meeting_id=mid, elapsed_s=761, lag_s=1.234, backlog=2, segments=84,
                          levels=kw.get("levels", {"mic": 0.5, "system": 0.0}))
    return st


def test_header_while_recording_and_stopping(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    h = logic.header_view(mid, m, recording_state(mid))
    assert (h.dot, h.text) == ("recording", "12:41 · 2 lines · live")
    st = recording_state(mid)
    st.phase = "stopping"
    assert logic.header_view(mid, m, st) == logic.HeaderView("busy", "Finishing live transcript…")


def test_status_bar_while_recording_shows_levels_lag_backlog(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    v = logic.status_view(m.meeting_id, m, recording_state(m.meeting_id))
    assert v.show_stop and v.stop_enabled
    assert v.mic.count("▮") == 4 and v.call == "▯" * 5  # 0.5 = -6 dB; silent
    assert v.text == "Lag 1.2 s   Backlog 2"


def test_processing_shows_stage_percent_and_time_left(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_PROCESSING)
    st = AppState(phase="ready", last_meeting=m.meeting_id)
    st.finalize = FinalizeProgress(m.meeting_id, "Identifying speakers (system)", 0.4, 0.35, 120.0, 390.0)
    h = logic.header_view(m.meeting_id, m, st)
    assert (h.dot, h.text) == ("busy", "Refining speaker labels")
    v = logic.status_view(m.meeting_id, m, st)
    assert v.text == "Identifying speakers (system) 40% · 35% overall · about 6 min left"
    assert not v.show_stop
    assert logic.notice_view(m.meeting_id, m, st).text == "Provisional: speaker labels may change."
    st.finalize = FinalizeProgress(m.meeting_id, "Identifying speakers")  # first meeting on the PC: no estimate
    assert logic.status_view(m.meeting_id, m, st).text == "Identifying speakers"


def test_the_progress_of_another_meeting_does_not_show_here(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_PROCESSING)
    st = AppState(phase="ready")
    st.finalize = FinalizeProgress("other", "Grouping voices", 0.5)
    assert logic.meeting_phase(m.meeting_id, m, st) == logic.WAITING
    assert logic.header_view(m.meeting_id, m, st).text == "Waiting for the final pass"


def test_final_recovered_and_failed_meetings(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 0, 60_000, "You", "x.")], final=[("mic", 0, 60_000, "You", "x.")])
    m = TranscriptModel.load(folder)
    assert logic.header_view(folder.meeting_id, m, AppState()).text.startswith("Final · ")
    assert logic.notice_view(folder.meeting_id, m, AppState()) is None
    folder.update_meta(clean_exit=False)
    m = TranscriptModel.load(folder)
    assert logic.header_view(folder.meeting_id, m, AppState()).text.startswith("Recovered after a crash · final")
    failed = make_meeting(tmp_path, [("mic", 0, 1000, "You", "x.")], status=mt.STATUS_FAILED, title="bad")
    failed.update_meta(error="out of memory")
    fm = TranscriptModel.load(failed)
    assert logic.header_view(failed.meeting_id, fm, AppState()) == logic.HeaderView("error", "Final pass failed")
    n = logic.notice_view(failed.meeting_id, fm, AppState())
    assert n.level == "error" and "out of memory" in n.text


def test_format_eta():
    f = logic.format_eta
    assert f(None) == "" and f(20) == "less than a minute left"
    assert f(60) == "about 1 min left" and f(6 * 60 + 10) == "about 6 min left"
    assert f(23 * 60) == "about 25 min left" and f(95 * 60) == "about 1 h 35 min left"
    assert f(120 * 60) == "about 2 h left"


def test_level_bars_is_a_log_meter():
    b = logic.level_bars
    assert b(0.0) == "▯" * 5 and b(0.005) == "▯" * 5
    assert b(1.0) == "▮" * 5 and b(5.0) == "▮" * 5
    assert b(0.2).count("▮") == 3 and b(0.03).count("▮") == 1


# -- picker ----------------------------------------------------------------------------
def test_list_meetings_newest_first_and_default_meeting(tmp_path):
    a = make_meeting(tmp_path, [("mic", 0, 1000, "You", "x.")], title="old",
                     started_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc), final=[("mic", 0, 1000, "You", "x.")])
    b = make_meeting(tmp_path, [("mic", 0, 1000, "You", "x.")], title="new",
                     started_at=datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc), status=mt.STATUS_PROCESSING)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "not-a-meeting").mkdir()
    got = logic.list_meetings(tmp_path)
    assert [e.title for e in got] == ["new", "old"]
    assert got[0].final is False and got[1].final is True and got[1].lines == 1
    assert "[final, 1 lines]" in got[1].label and "2026-01-01 09:00" in got[1].label
    assert logic.default_meeting(tmp_path).path == b.path  # newest
    assert logic.default_meeting(tmp_path, last_id=a.meeting_id).path == a.path
    assert logic.default_meeting(tmp_path, finalizing_id=a.meeting_id, last_id=b.meeting_id).path == a.path
    assert logic.default_meeting(tmp_path, recording=b).path == b.path
    assert logic.default_meeting(tmp_path / "none") is None


def test_export_text_clipboard_has_no_crlf(tmp_path):
    m = model_for(tmp_path)
    assert "\r" not in logic.export_text(m, "txt", "elapsed", True, clipboard=True)
    assert "\r\n" in logic.export_text(m, "txt", "elapsed", True)


# -- replay ---------------------------------------------------------------------------------
def test_next_step_of_the_unattended_replay():
    def step(**kw):
        base = dict(phase="ready", started=False, stopped=False, audio_finished=False, finalizing=False,
                    finalize_queue=0)
        return next_step(**{**base, **kw})

    assert step(phase="downloading") is None
    assert step() == "start"
    assert step(started=True, phase="starting") is None
    assert step(started=True, phase="recording") is None
    assert step(started=True, phase="recording", audio_finished=True) == "stop"
    assert step(started=True, stopped=True, phase="stopping", audio_finished=True) is None
    assert step(started=True, stopped=True, audio_finished=True, finalize_queue=1, finalizing=True) is None
    assert step(started=True, stopped=True, audio_finished=True, finalize_queue=1) is None
    assert step(started=True, stopped=True, audio_finished=True) == "quit"


def test_replay_files_and_output_guard(tmp_path):
    src = tmp_path / "src"
    (src / "audio").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        replay_files(src)
    (src / "audio" / "system.flac").write_bytes(b"x")
    assert list(replay_files(src)) == ["system"]
    check_output_dir(src, tmp_path / "out")
    check_output_dir(src, tmp_path)  # the parent is fine: a new sibling folder is created
    with pytest.raises(ValueError):
        check_output_dir(src, src)
    with pytest.raises(ValueError):
        check_output_dir(src, src / "audio")
    r = Replay.create(src, tmp_path / "out", title="t", auto=True)
    assert r.auto and not r.audio_finished and list(r.files) == ["system"]


# -- setting + controller --------------------------------------------------------------------------
def test_open_window_setting_defaults_on_and_is_additive():
    assert config.Settings().open_window_on_start is True
    old = config.Settings.from_dict({"settings_version": 4, "whisper_model": "base.en"})
    assert old.open_window_on_start is True and old.settings_version == config.SETTINGS_VERSION
    assert config.Settings.from_dict({"open_window_on_start": False}).open_window_on_start is False


def test_controller_overrides_apply_but_are_not_saved(tmp_path):
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path / "lab")})
    assert ctl.settings.resolved_meetings_dir() == tmp_path / "lab"
    new = config.Settings.from_dict(ctl.settings.to_dict())
    new.keep_audio = True
    ctl.update_settings(new)
    assert ctl.settings.keep_audio and ctl.settings.resolved_meetings_dir() == tmp_path / "lab"
    saved = config.load()
    assert saved.keep_audio is True and saved.meetings_dir == ""  # the override never reaches settings.json
    assert ctl.current_folder() is None


def test_theme_colours_are_readable_on_white():
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(fg, bg):
        a, b = sorted((lum(fg), lum(bg)), reverse=True)
        return (a + 0.05) / (b + 0.05)

    for color in (*theme.SPEAKER_COLORS, theme.TEXT, theme.MUTED, *theme.KIND_COLORS.values()):
        assert ratio(color, "#ffffff") >= 4.5, color
    for fg, bg in ((theme.MUTE_BAR_FG, theme.MUTE_BAR_BG), (theme.NOTICE_FG, theme.NOTICE_BG),
                   (theme.FAILED_FG, theme.FAILED_BG), (theme.TEXT, theme.CHANGED_BG)):
        assert ratio(fg, bg) >= 4.5, (fg, bg)
    assert theme.speaker_color(8) == theme.SPEAKER_COLORS[0] and theme.speaker_color(3, is_you=True) == theme.YOU_COLOR


# -- bookmarks and mute (WP5) ------------------------------------------------------------------
def muted_state(mid, muted=True):
    st = recording_state(mid)
    st.recording.mic_muted = muted
    return st


def test_status_view_says_muted_in_place_of_the_mic_meter(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    v = logic.status_view(m.meeting_id, m, muted_state(m.meeting_id))
    assert v.mic == "muted" and v.mic_muted and v.call and v.stop_enabled
    v = logic.status_view(m.meeting_id, m, muted_state(m.meeting_id, False))
    assert v.mic != "muted" and not v.mic_muted


def test_mute_bar_and_button_follow_the_displayed_meeting(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    bar = logic.mute_bar_view(mid, m, muted_state(mid))
    assert bar.label == "MIC MUTED" and "the call still hears you" in bar.text and "room" not in bar.text
    assert logic.mute_bar_view(mid, m, muted_state(mid, False)) is None
    assert logic.mute_bar_view("another-meeting", m, muted_state(mid)) is None  # a different meeting is open
    btn = logic.mute_button_view(mid, m, muted_state(mid, False), "Ctrl+Alt+M")
    assert btn.visible and btn.text == "Mute me in the recording" and "[Ctrl+Alt+M]" in btn.tooltip
    assert "call app still hears you" in btn.tooltip
    assert logic.mute_button_view(mid, m, muted_state(mid), "").text == "Unmute me"
    idle = AppState(phase="ready")
    assert not logic.mute_button_view(mid, m, idle).visible
    stopping = AppState(phase="stopping", last_meeting=mid)
    assert not logic.mute_button_view(mid, m, stopping).visible


def test_in_room_mode_warns_that_everyone_is_muted(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    m.set_meta({**m.meta, "settings": {"diarize_mic": True}})
    mid = m.meeting_id
    assert "Everyone in the room" in logic.mute_bar_view(mid, m, muted_state(mid)).text
    assert "everyone in the room is muted" in logic.mute_button_view(mid, m, muted_state(mid)).tooltip


def test_bookmark_items_list_the_four_types_with_symbol_and_hotkey():
    items = logic.bookmark_items({"bookmark": "Ctrl+Alt+B", "decision": "Ctrl+Alt+D", "action": "", "question": "Ctrl+Alt+Q"})
    assert [i.kind for i in items] == ["bookmark", "decision", "action", "question"]
    assert [i.label for i in items] == ["⚑ Bookmark", "✔ Decision", "☐ Action item", "? Question"]
    assert items[1].menu_text == "✔ Decision    Ctrl+Alt+D" and items[2].menu_text == "☐ Action item"
    assert logic.bookmark_items()[0].hotkey == ""
    assert logic.bookmark_items(None, {"risk": "Risk"})[-1].label.endswith("Risk")


def test_mark_changes_and_feedback():
    row = MarkRow(3, "decision", "Ship it", 100)
    assert logic.mark_changes(row, "Ship it", "decision") == {}
    assert logic.mark_changes(row, "  Ship it ", "decision") == {}
    assert logic.mark_changes(row, "", "action") == {"label": "", "kind": "action"}
    assert logic.mark_changes(row, "New", "decision") == {"label": "New"}
    assert logic.bookmark_feedback({"action": "added", "kind": "decision"}) == "Decision added"
    assert logic.bookmark_feedback({"action": "retyped", "kind": "question"}) == "Changed to Question"
    assert logic.bookmark_feedback({"action": "ignored", "kind": "bookmark"}) == "Bookmark already added"
    assert logic.bookmark_feedback({"action": "none"}) == ""


def test_tray_tooltip_counts_bookmarks_and_says_muted():
    assert logic.recording_status_text(761, 84) == "● Recording 12:41 · 84 lines"
    assert logic.recording_status_text(761, 84, marks=1) == "● Recording 12:41 · 84 lines · 1 bookmark"
    assert logic.recording_status_text(3725, 3, 6.4, 3, True) == (
        "● Recording 1:02:05 · 3 lines · 6s behind · 3 bookmarks · your mic is muted")


def test_hotkey_row_status_wording():
    from huddle_helper import hotkeys as hk

    f = logic.hotkey_row_status
    ok = hk.check_combo("Ctrl+Alt+B", lambda c: None)
    assert f(hk.check_combo(""), None) == ("Off", "info")
    assert f(hk.check_combo("B"), None)[1] == "error"
    assert f(ok, None)[1] == "info" and "Check" in f(ok, None)[0]
    assert f(ok, hk.Registration("Ctrl+Alt+B", True), live=True) == ("Registered now.", "ok")
    assert f(ok, hk.Registration("Ctrl+Alt+B", True))[0].startswith("Free")
    text, level = f(ok, hk.Registration("Ctrl+Alt+B", False, "already in use by another program"))
    assert level == "error" and "already in use by another program" in text
    assert f(ok, None, duplicate_of="Decision")[1] == "error"
    altgr = hk.check_combo("Ctrl+Alt+Q", lambda c: "@" if "Shift" not in c.text else None)
    text, level = f(altgr, hk.Registration("Ctrl+Alt+Q", True))
    assert level == "warn" and "AltGr" in text and "Ctrl+Alt+Shift+Q" in text and "Free" in text


def test_notice_names_hotkeys_that_could_not_be_registered(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    st = recording_state(m.meeting_id)
    assert logic.notice_view(m.meeting_id, m, st).text.count("\n") == 0
    st.hotkey_problems = ["Ctrl+Alt+B (Bookmark): already in use by another program"]
    text = logic.notice_view(m.meeting_id, m, st).text
    assert "Hotkeys not working: Ctrl+Alt+B (Bookmark)" in text and "buttons and the tray menu" in text


def test_tray_icons_for_muted_and_flag_differ_from_the_recording_icon():
    from huddle_helper import tray

    imgs = {k: tray.make_icon(k) for k in ("recording", "muted", "flag", "idle")}
    assert all(i.size == (64, 64) for i in imgs.values())
    rec, muted, flag = (imgs[k].tobytes() for k in ("recording", "muted", "flag"))
    assert len({rec, muted, flag}) == 3
    # the muted icon is the recording icon plus a slash, so its background colour is the same red
    assert imgs["muted"].getpixel((5, 32)) == imgs["recording"].getpixel((5, 32)) == (220, 38, 38, 255)


# -- "This line is…" on a line too short to fingerprint (#26) ---------------------------
SHORT_NOTE = "Too short to recognise the voice: the name applies to this line only."


def lines_of(tmp_path, *lengths_ms):
    rows = [("system", 10_000 * i, 10_000 * i + n, "SPEAKER_00", f"line {i}.") for i, n in enumerate(lengths_ms)]
    return TranscriptModel.load(make_meeting(tmp_path, rows))


def test_a_line_under_half_a_second_says_its_name_cannot_steer_the_voices(tmp_path):
    m = lines_of(tmp_path, 300, 499, 500, 2000)
    assert [logic.short_line_note(r) for r in m.lines] == [SHORT_NOTE, SHORT_NOTE, "", ""]
    menus = [logic.line_name_menu(m, r) for r in m.lines]
    assert [nm.hint for nm in menus] == [SHORT_NOTE, SHORT_NOTE, "", ""]
    assert all(nm.enabled and nm.note == "" for nm in menus)


def test_the_short_line_note_follows_the_fingerprint_minimum(tmp_path, monkeypatch):
    from huddle_helper import diarize

    monkeypatch.setattr(diarize, "MIN_FINGERPRINT_S", 1.0)
    m = lines_of(tmp_path, 700, 1000)
    assert [logic.short_line_note(r) for r in m.lines] == [SHORT_NOTE, ""]
