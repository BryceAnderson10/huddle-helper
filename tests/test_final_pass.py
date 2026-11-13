"""The final pass as one module (#61): ``FinalPass.run(folder, on_progress)``, its typed progress, the
meeting's own Keep audio choice and re-running."""

import threading
import time

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import config, final_pass as fp, library, meeting as mt

from .conftest import FakeDiarizer, ScriptedTranscriber, make_meeting_to_finalize
from .helpers import make_meeting


def final_pass(**kw):
    from huddle_helper.final_pass import FinalPass

    settings = kw.pop("settings", None) or config.Settings()
    kw.setdefault("diarizer_factory", FakeDiarizer)
    kw.setdefault("embedder_factory", None)
    kw.setdefault("transcriber_factory", None)
    return FinalPass(settings, **kw)


def test_progress_never_goes_back_with_the_second_transcription_on(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    seen = []
    final_pass(transcriber_factory=ScriptedTranscriber).run(folder, seen.append)
    overall = [p.overall for p in seen]
    assert overall == sorted(overall), [(p.stage.label, p.overall) for p in seen]
    assert overall[0] == 0.0 and overall[-1] == 1.0
    labels = [p.stage.label for p in seen]
    assert "Improving the transcript" in labels and "Identifying speakers (system)" in labels
    assert labels.index("Improving the transcript") < labels.index("Identifying speakers (system)")


def test_without_the_second_transcription_progress_starts_at_speakers(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    seen = []
    final_pass().run(folder, seen.append)
    assert [p.label for p in seen] == ["Identifying speakers (system)", "Identifying speakers (system)",
                                      "Identifying speakers (system)", "Grouping voices", "Writing final transcript"]
    assert [p.overall for p in seen] == pytest.approx([0.0, 0.0, 0.87, 0.87, 1.0])
    assert all(p.meeting_id == folder.meeting_id for p in seen)


# -- Keep audio: the meeting's own choice ---------------------------------------------------------
@pytest.mark.parametrize("own, setting, kept", [(False, True, False), (True, False, True)])
def test_the_meetings_own_keep_audio_choice_wins_over_the_setting(tmp_path, own, setting, kept):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=own)
    final_pass(settings=config.Settings(keep_audio=setting)).run(folder)
    assert folder.audio_file("system").exists() is kept
    assert folder.read_meta()["audio_kept"] is kept


@pytest.mark.parametrize("setting", [False, True])
def test_a_meeting_without_its_own_choice_follows_the_setting(tmp_path, setting):
    folder = make_meeting_to_finalize(tmp_path)
    meta = folder.read_meta()
    del meta["settings"]["keep_audio"]
    mt.write_json_atomic(folder.meta_path, meta)
    final_pass(settings=config.Settings(keep_audio=setting)).run(folder)
    assert folder.audio_file("system").exists() is setting


# -- re-running -----------------------------------------------------------------------------------
def test_running_again_replaces_the_final_transcript(tmp_path):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=True)
    final_pass().run(folder)
    first = folder.read_meta()["transcript_version"]
    seen_done = []
    final_pass().run(folder, lambda p: seen_done.append(folder.is_done))
    assert not any(seen_done[1:]), "DONE is cleared while the final transcript is replaced"
    assert folder.is_done and folder.read_meta()["transcript_version"] != first


# -- streams and time left ------------------------------------------------------------------------
def shared_mic_meeting(tmp_path, stretches, duration_s):
    folder = make_meeting_to_finalize(tmp_path)
    sf.write(str(folder.audio_file("mic")), np.zeros(16000 * 3, np.float32), 16000, format="FLAC")
    folder.update_meta(shared_mic=stretches, duration_s=duration_s)
    return folder


def test_the_mic_counts_by_its_shared_mic_stretches(tmp_path):
    # 600 s of shared mic and 7200 s of call audio: the mic is 600/7800 of the separation
    folder = shared_mic_meeting(tmp_path, [[0, 600_000]], 7200)
    seen = []
    final_pass().run(folder, seen.append)
    at = {(p.label, p.frac): p.overall for p in seen}
    assert at[("Identifying speakers (mic)", 1.0)] == pytest.approx(0.87 * 600 / 7800)
    assert at[("Identifying speakers (system)", 0.0)] == pytest.approx(0.87 * 600 / 7800)
    assert at[("Identifying speakers (system)", 1.0)] == pytest.approx(0.87)
    overall = [p.overall for p in seen]
    assert overall == sorted(overall)


def test_no_shared_mic_and_no_call_separation_goes_straight_to_grouping(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    folder.update_meta_settings(diarize_system=False)
    seen = []
    final_pass().run(folder, seen.append)
    assert seen[0].label == "Grouping voices"
    assert [p.overall for p in seen] == pytest.approx([0.0, 0.0, 1.0])


def test_time_left_comes_from_earlier_passes_on_this_pc(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    folder.update_meta(duration_s=1000)
    seen = []
    final_pass().run(folder, seen.append)
    assert all(p.eta_s is None for p in seen)  # the first meeting on the PC: no estimate

    other = make_meeting_to_finalize(tmp_path)
    other.update_meta(duration_s=1000)
    folder.update_meta(final_stats={"speed": 0.2})  # 200 s expected for 1000 s of audio
    seen = []
    final_pass(clock=lambda: 50.0).run(other, seen.append)
    assert seen[0].eta_s == pytest.approx(200.0) and seen[0].elapsed_s == 0.0
    assert seen[-1].eta_s == 0.0


# -- the app's worker runs the FinalPass it is given ----------------------------------------------
class FakeFinalPass:
    """Reports two steps, letting the test look at the app between them, then finishes or fails."""

    def __init__(self, fail=False):
        self.fail = fail
        self.during: list = []
        self.ran = threading.Event()

    def run(self, folder, on_progress=None):
        from huddle_helper.final_pass import PassProgress
        from huddle_helper.finalize import Stage, Step

        on_progress(PassProgress(folder.meeting_id, Stage(Step.SECOND_TRANSCRIPTION), 0.0, 0.0))
        self.during.append(self.app.live_state().progress)
        on_progress(PassProgress(folder.meeting_id, Stage(Step.SEPARATE, "system"), 1.0, 0.9, 3.0, 1.0))
        self.during.append((self.app.state.finalizing, self.app.state.finalize.label,
                            self.app.live_state().progress))
        try:
            if self.fail:
                raise RuntimeError("model broke")
            folder.mark_done()
            return 3
        finally:
            self.ran.set()


def run_in_app(tmp_path, fake):
    from huddle_helper import app

    from .helpers import make_meeting

    folder = make_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "x.")], status=mt.STATUS_FAILED)
    notes = []
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)}, notify=notes.append, final_pass=fake)
    fake.app = ctl
    assert ctl.retry_final(folder.meeting_id) == "started"
    assert fake.ran.wait(5)
    deadline = time.monotonic() + 5
    while ctl.is_busy(folder.meeting_id) and time.monotonic() < deadline:
        time.sleep(0.01)
    return ctl, folder, notes


def test_the_app_shows_the_final_pass_progress_and_tells_when_it_is_ready(tmp_path):
    fake = FakeFinalPass()
    ctl, folder, notes = run_in_app(tmp_path, fake)
    assert fake.during == [0.0, ("Identifying speakers (system) 100%", "Identifying speakers (system)", 0.9)]
    assert notes == [f"Final transcript ready: {folder.meeting_id}"]
    assert ctl.state.finalize is None and ctl.state.finalizing == "" and ctl.state.finalize_queue == 0


def test_the_app_marks_a_failed_final_pass(tmp_path):
    ctl, folder, notes = run_in_app(tmp_path, FakeFinalPass(fail=True))
    meta = folder.read_meta()
    assert meta["status"] == mt.STATUS_FAILED and meta["error"] == "model broke"
    assert notes == [f"Could not finish {folder.meeting_id}: model broke"]
    assert ctl.state.finalize is None and not ctl.is_busy(folder.meeting_id)


# -- the CLI runs the same final pass ---------------------------------------------------------------
@pytest.fixture()
def cli_with_fakes(monkeypatch):
    """``huddle-helper`` with the real FinalPass and fake models."""
    from huddle_helper import cli

    class WithFakes(fp.FinalPass):
        def __init__(self, settings, **kw):
            super().__init__(settings, diarizer_factory=FakeDiarizer, embedder_factory=None,
                             transcriber_factory=None)

    monkeypatch.setattr(fp, "FinalPass", WithFakes)
    monkeypatch.setattr(cli, "go_offline", lambda: None)
    return cli


@pytest.mark.parametrize("own", [False, True])
def test_cli_finalize_follows_the_meetings_own_keep_audio_choice(tmp_path, cli_with_fakes, own):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=own)
    assert cli_with_fakes.main(["finalize", str(folder.path), "--out", str(tmp_path)]) == 0
    assert folder.is_done and folder.audio_file("system").exists() is own


@pytest.mark.parametrize("flag, kept", [("--keep-audio", True), ("--delete-audio", False)])
def test_cli_finalize_flags_change_the_meetings_own_choice(tmp_path, cli_with_fakes, flag, kept):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=not kept)
    assert cli_with_fakes.main(["finalize", str(folder.path), "--out", str(tmp_path), flag]) == 0
    assert folder.audio_file("system").exists() is kept
    assert folder.read_meta()["settings"]["keep_audio"] is kept


def test_cli_finalize_again_replaces_the_final_transcript(tmp_path, cli_with_fakes):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=True)
    assert cli_with_fakes.main(["finalize", str(folder.path)]) == 0
    first = folder.read_meta()["transcript_version"]
    assert cli_with_fakes.main(["finalize", str(folder.path)]) == 0
    assert folder.is_done and folder.read_meta()["transcript_version"] != first


def test_cli_recover_follows_the_meetings_own_keep_audio_choice(tmp_path, cli_with_fakes):
    folder = make_meeting_to_finalize(tmp_path, keep_audio=False)  # stopped, its final pass never ran
    assert cli_with_fakes.main(["recover", "--out", str(tmp_path), "--keep-audio"]) == 0
    assert folder.is_done and not folder.audio_file("system").exists()


# -- the estimate -----------------------------------------------------------
def test_no_history_means_no_estimate():
    assert fp.estimate_remaining_s(0.5, 100, 600, []) is None
    assert fp.estimate_remaining_s(0.0, 0, None, [0.2]) is None


def test_before_ten_percent_uses_history_only():
    # median speed 0.2 x 1000 s of audio = 200 s expected; 5% done leaves 95%
    assert fp.estimate_remaining_s(0.05, 3, 1000, [0.1, 0.2, 0.9]) == pytest.approx(190.0)
    assert fp.estimate_remaining_s(0.10, 50, 1000, [0.2]) == pytest.approx(180.0)  # exactly 10%: not yet


def test_past_ten_percent_blends_in_the_passes_own_rate():
    # expected from history 200 s; own rate says 20 s elapsed at 40% = 50 s... no: 20/0.4 = 50 total.
    est = fp.estimate_remaining_s(0.4, 20, 1000, [0.2])
    expected = 0.6 * 200 + 0.4 * 50
    assert est == pytest.approx(0.6 * expected)
    # a pass running twice as slow as history pulls the estimate up as it goes on
    slow = fp.estimate_remaining_s(0.8, 320, 1000, [0.2])  # own total 400 s
    assert slow > fp.estimate_remaining_s(0.8, 160, 1000, [0.2])
    assert slow == pytest.approx(0.2 * (0.2 * 200 + 0.8 * 400))


def test_done_leaves_nothing():
    assert fp.estimate_remaining_s(1.0, 300, 1000, [0.2]) == 0.0


def test_median_history_ignores_one_odd_meeting():
    a = fp.estimate_remaining_s(0.0, 0, 100, [0.2, 0.2, 0.2, 0.2, 5.0])
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
    speeds = fp.recent_final_speeds(tmp_path)
    assert speeds == pytest.approx([0.3, 0.4, 0.5, 0.6, 0.7])
    one = next(mt.iter_meetings(tmp_path))
    assert 0.1 not in [round(s, 1) for s in fp.recent_final_speeds(tmp_path, exclude=one.meeting_id)]
    assert fp.recent_final_speeds(tmp_path / "missing") == []


def test_meeting_length_prefers_recorded_duration():
    assert library.meeting_length_s({"duration_s": 61.5}) == 61.5
    assert library.meeting_length_s({"started_at": "2026-01-01T10:00:00+00:00",
                                 "ended_at": "2026-01-01T10:01:30+00:00"}) == 90
    assert library.meeting_length_s({}) is None
