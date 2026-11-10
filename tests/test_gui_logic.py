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
