import numpy as np
import pytest

from huddle_helper import app, config, meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.recorder import Components, Recorder, Status
from huddle_helper.vad import EnergyScorer

from .conftest import ToneEmbedder, ToneTranscriber
from .helpers import make_meeting


# -- stage -> fraction of the pass ------------------------------------------
def test_pass_fraction_single_stream():
    f = app.pass_fraction
    assert f("Identifying speakers (system)", 0.0, ["system"]) == 0.0
    assert f("Identifying speakers (system)", 1.0, ["system"]) == pytest.approx(app.DIARIZE_SHARE)
    assert f("Grouping voices", 0.0, ["system"]) == pytest.approx(app.DIARIZE_SHARE)
    assert f("Grouping voices", 1.0, ["system"]) == 1.0
    assert f("Writing final transcript", 1.0, ["system"]) == 1.0


def test_pass_fraction_two_streams_and_no_diarization():
    f = app.pass_fraction
    both = ["mic", "system"]
    assert f("Identifying speakers (mic)", 0.5, both) == pytest.approx(app.DIARIZE_SHARE * 0.25)
    assert f("Identifying speakers (system)", 0.5, both) == pytest.approx(app.DIARIZE_SHARE * 0.75)
    assert f("Grouping voices", 0.5, []) == 0.5
    assert f("Identifying speakers (system)", 7.0, ["system"]) == pytest.approx(app.DIARIZE_SHARE)  # clamped


# -- the estimate -----------------------------------------------------------
def test_no_history_means_no_estimate():
    assert app.estimate_remaining_s(0.5, 100, 600, []) is None
    assert app.estimate_remaining_s(0.0, 0, None, [0.2]) is None


def test_before_ten_percent_uses_history_only():
    # median speed 0.2 x 1000 s of audio = 200 s expected; 5% done leaves 95%
    assert app.estimate_remaining_s(0.05, 3, 1000, [0.1, 0.2, 0.9]) == pytest.approx(190.0)
    assert app.estimate_remaining_s(0.10, 50, 1000, [0.2]) == pytest.approx(180.0)  # exactly 10%: not yet


def test_past_ten_percent_blends_in_the_passes_own_rate():
    # expected from history 200 s; own rate says 20 s elapsed at 40% = 50 s... no: 20/0.4 = 50 total.
    est = app.estimate_remaining_s(0.4, 20, 1000, [0.2])
    expected = 0.6 * 200 + 0.4 * 50
    assert est == pytest.approx(0.6 * expected)
    # a pass running twice as slow as history pulls the estimate up as it goes on
    slow = app.estimate_remaining_s(0.8, 320, 1000, [0.2])  # own total 400 s
    assert slow > app.estimate_remaining_s(0.8, 160, 1000, [0.2])
    assert slow == pytest.approx(0.2 * (0.2 * 200 + 0.8 * 400))


def test_done_leaves_nothing():
    assert app.estimate_remaining_s(1.0, 300, 1000, [0.2]) == 0.0


def test_median_history_ignores_one_odd_meeting():
    a = app.estimate_remaining_s(0.0, 0, 100, [0.2, 0.2, 0.2, 0.2, 5.0])
    assert a == pytest.approx(20.0)


# -- history and length from meeting folders ----------------------------------
def finished(tmp_path, name, speed, written, status=mt.STATUS_COMPLETE):
    f = make_meeting(tmp_path, [("mic", 0, 1000, "You", "x.")], title=name, status=status)
    f.update_meta(final_stats={"speed": speed}, final_written_at=written)
    return f


def test_recent_final_speeds_takes_the_last_five_completed(tmp_path):
    for i in range(7):
        finished(tmp_path, f"m{i}", 0.1 * (i + 1), f"2026-01-0{i + 1}T10:00:00")
    finished(tmp_path, "failed", 9.0, "2026-01-09T10:00:00", status=mt.STATUS_FAILED)
    finished(tmp_path, "nospeed", None, "2026-01-09T11:00:00")
    speeds = app.recent_final_speeds(tmp_path)
    assert speeds == pytest.approx([0.3, 0.4, 0.5, 0.6, 0.7])
    one = next(mt.iter_meetings(tmp_path))
    assert 0.1 not in [round(s, 1) for s in app.recent_final_speeds(tmp_path, exclude=one.meeting_id)]
    assert app.recent_final_speeds(tmp_path / "missing") == []


def test_meeting_length_prefers_recorded_duration():
    assert app.meeting_length_s({"duration_s": 61.5}) == 61.5
    assert app.meeting_length_s({"started_at": "2026-01-01T10:00:00+00:00",
                                 "ended_at": "2026-01-01T10:01:30+00:00"}) == 90
    assert app.meeting_length_s({}) is None


def test_app_state_has_finalize_progress_and_keeps_the_string():
    st = app.AppState()
    assert st.finalize is None and st.finalizing == ""
    p = app.FinalizeProgress("m", "Grouping voices", 0.5, 0.9, 12.0, 30.0)
    assert (p.meeting_id, p.stage, p.frac, p.overall, p.elapsed_s, p.eta_s) == ("m", "Grouping voices", 0.5, 0.9, 12.0, 30.0)


# -- levels -------------------------------------------------------------------
def recorder(tmp_path):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"))
    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer)
    return Recorder(s, comps)


def test_status_has_levels_for_both_streams():
    assert Status().levels == {"mic": 0.0, "system": 0.0}
    assert Status().levels is not Status().levels


def test_levels_follow_the_peak_and_decay(tmp_path, monkeypatch):
    rec = recorder(tmp_path)
    clock = [100.0]
    monkeypatch.setattr("huddle_helper.recorder.time.perf_counter", lambda: clock[0])
    loud = np.full(1600, 0.0, np.float32)
    loud[10] = -0.8
    rec._on_audio("mic", loud)
    assert rec._current_levels() == {"mic": 0.8, "system": 0.0}
    clock[0] += rec.LEVEL_HALF_LIFE_S
    assert rec._current_levels()["mic"] == pytest.approx(0.4, abs=0.001)
    rec._on_audio("mic", np.full(1600, 0.1, np.float32))  # quieter chunk: the held, decayed peak stays
    assert rec._current_levels()["mic"] == pytest.approx(0.4, abs=0.001)
    clock[0] += 10 * rec.LEVEL_HALF_LIFE_S
    assert rec._current_levels()["mic"] < 0.001
    rec._on_audio("system", np.zeros(0, np.float32))  # empty chunk is harmless
    assert rec._current_levels()["system"] == 0.0


def test_published_status_carries_levels(tmp_path):
    rec = recorder(tmp_path)
    seen = []
    rec._on_status = seen.append
    rec.status = Status(state="recording")
    rec._t0 = 1.0
    rec._on_audio("system", np.full(160, 0.5, np.float32))
    rec._publish()
    assert seen[-1].levels["system"] > 0.4 and seen[-1].levels["mic"] == 0.0
