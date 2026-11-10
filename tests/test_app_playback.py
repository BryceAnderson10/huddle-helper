"""AppController side of click to hear (WP7): play_clip, the headphone rules, stop, Keep this meeting's audio."""

import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import app, finalize, meeting as mt, playback
from huddle_helper.audio.ring import RecentAudio

from .conftest import tone
from .helpers import make_meeting
from .test_library import LINES


class FakePlayer:
    """Never touches a sound card: remembers what it was asked to play."""

    gain = 1.0

    def __init__(self):
        self.played, self.stops, self._done = [], 0, None
        self.started = threading.Event()

    def play(self, samples, on_done=None):
        self.played.append(np.array(samples))
        self._done = on_done
        self.started.set()

    def stop(self):
        self.stops += 1

    def finish(self, reason="finished"):
        done, self._done = self._done, None
        done(reason)


@pytest.fixture()
def ctl(tmp_path):
    c = app.AppController(overrides={"meetings_dir": str(tmp_path)}, player=FakePlayer())
    c._jobs = queue.Queue()
    c.notes = []
    c._notify = c.notes.append
    return c


def kept(tmp_path, seconds=12.0, **kw):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING, **kw)
    for src in ("mic", "system"):
        sf.write(str(folder.audio_file(src)), tone(300, seconds), 16_000, format="FLAC", subtype="PCM_16")
    return folder


def clip(source="system", start=2000, end=3000, text="hello there"):
    return playback.Clip(playback.line_key(source, start), (source,), start, end, text)


def test_a_finished_meeting_plays_from_the_files_and_the_key_follows_the_clip(ctl, tmp_path):
    folder = kept(tmp_path)
    res = ctl.play_clip(folder, clip())
    assert res.status == "playing" and res.notice == "" and ctl.playing_key == "line:system:2000"
    assert ctl.player.started.wait(5)
    assert len(ctl.player.played[0]) == int(1.6 * 16_000)  # padded, read on a thread
    ctl.player.finish()
    assert ctl.playing_key is None


def test_clicking_another_clip_stops_the_current_one_and_stale_reads_do_not_start(ctl, tmp_path):
    folder = kept(tmp_path)
    ctl.play_clip(folder, clip(start=2000))
    ctl.player.started.wait(5)
    old_done = ctl.player._done
    ctl.play_clip(folder, clip(start=6000))
    assert ctl.player.stops >= 2 and ctl.playing_key == "line:system:6000"
    old_done("stopped")  # the first clip reports in late: it must not clear the second
    assert ctl.playing_key == "line:system:6000"
    ctl.stop_playback()
    assert ctl.playing_key is None
    time.sleep(0.2)  # a read still in flight must not start playing after Stop
    n = len(ctl.player.played)
    ctl.play_clip(folder, clip(start=7000))
    ctl.stop_playback()
    time.sleep(0.3)
    assert len(ctl.player.played) in (n, n + 1) and ctl.playing_key is None


def test_a_clip_that_cannot_be_read_says_so_and_clears_the_playing_state(ctl, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)  # no audio at all
    ctl.play_clip(folder, clip())
    deadline = time.monotonic() + 5
    while ctl.playing_key and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ctl.playing_key is None and ctl.notes == ["Could not read that audio."] and ctl.player.played == []


def test_a_device_error_is_reported(ctl, tmp_path):
    folder = kept(tmp_path)
    ctl.play_clip(folder, clip())
    ctl.player.started.wait(5)
    ctl.player.finish("error: no output device")
    assert ctl.playing_key is None and any("no output device" in n for n in ctl.notes)


# -- while recording ---------------------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self, folder, echo=0, ring_ms=60_000):
        self.folder, self.echo_dropped, self.noted = folder, echo, []
        self.ring = RecentAudio(600)
        self.ring.write(tone(300, ring_ms / 1000))

    def recent_clip(self, clip):
        return playback.clip_from_ring(self.ring, clip.start_ms, clip.end_ms, clip.pad_ms)

    def note_playback(self, duration_s, text, run=False):
        self.noted.append((round(duration_s, 2), text) if not run else (round(duration_s, 2), text, "run"))
        return True

    def end_playback_run(self):
        self.noted.append("run ended")

    def recent_oldest_ms(self):
        return {"mic": self.ring.oldest_ms, "system": self.ring.oldest_ms}


def recording(ctl, tmp_path, **kw):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING)
    ctl._recorder = FakeRecorder(folder, **kw)
    ctl.state.phase = "recording"
    return folder


def test_while_recording_a_clip_comes_from_the_ring_and_opens_a_playback_window(ctl, tmp_path):
    folder = recording(ctl, tmp_path)
    res = ctl.play_clip(folder, clip(start=10_000, end=12_000, text="we ship friday"))
    assert res.status == "playing" and res.notice == playback.HEADPHONES  # the first clip of the meeting
    assert ctl._recorder.noted == [(2.6, "we ship friday")]
    assert len(ctl.player.played[0]) == int(2.6 * 16_000) and ctl.recent_oldest_ms(folder) == {"mic": 0, "system": 0}
    ctl.player.finish()
    again = ctl.play_clip(folder, clip(start=20_000, end=21_000))
    assert again.status == "playing" and again.notice == ""  # warned once per meeting, not every clip


def test_a_line_older_than_the_ring_is_not_played_and_nothing_is_logged(ctl, tmp_path):
    folder = recording(ctl, tmp_path)
    ctl._recorder.ring = RecentAudio(10)  # holds 10 s
    ctl._recorder.ring.write(tone(300, 30.0))  # 20 s .. 30 s
    res = ctl.play_clip(folder, clip(start=5_000, end=6_000))
    assert res.status == "unavailable" and "Available after Stop" in res.message
    assert ctl._recorder.noted == [] and ctl.player.played == [] and ctl.playing_key is None


def test_when_echo_dedupe_has_dropped_mic_lines_the_user_must_confirm_once(ctl, tmp_path):
    folder = recording(ctl, tmp_path, echo=3)
    res = ctl.play_clip(folder, clip(start=10_000, end=12_000))
    assert res.status == "confirm" and "Play it anyway?" in res.message
    assert ctl.player.played == [] and ctl.playing_key is None and ctl._recorder.noted == []
    res = ctl.play_clip(folder, clip(start=10_000, end=12_000), confirmed=True)
    assert res.status == "playing" and res.notice == playback.HEADPHONES
    ctl.player.finish()
    assert ctl.play_clip(folder, clip(start=20_000, end=21_000)).status == "playing"  # confirmed for this meeting
    ctl.player.finish()
    other = recording_other = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING, title="Other")
    ctl._recorder.folder = other
    assert ctl.play_clip(other, clip(start=10_000, end=12_000)).status == "confirm"  # a new meeting asks again


def test_nothing_is_logged_for_a_meeting_that_is_not_the_one_recording(ctl, tmp_path):
    live = recording(ctl, tmp_path)
    old = kept(tmp_path, title="Older")
    res = ctl.play_clip(old, clip())
    assert res.status == "playing" and res.notice == "" and ctl._recorder.noted == []
    assert ctl.recent_oldest_ms(old) is None and ctl.recent_oldest_ms(live) is not None


def test_the_player_is_stopped_before_the_capture_reopens_its_devices(ctl):
    assert ctl.player.stops == 0
    # the recorder is given the player's stop as its before_reopen (decision 7)
    from huddle_helper.recorder import Recorder

    seen = {}
    real_init = Recorder.__init__

    def spy(self, *a, **kw):
        seen["before_reopen"] = kw.get("before_reopen")
        real_init(self, *a, **kw)

    Recorder.__init__ = spy
    try:
        ctl._get_components = lambda: SimpleNamespace(capture_factory=None, transcriber=None)
        ctl.state.phase = "ready"
        ctl.start("x")
        time.sleep(0.3)
    finally:
        Recorder.__init__ = real_init
    assert seen["before_reopen"] == ctl.player.stop
    ctl.shutdown()


# -- Keep this meeting's audio -------------------------------------------------------------------------------


def test_set_keep_audio_writes_the_meetings_own_setting_and_keeps_the_others(ctl, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)
    folder.update_meta(settings={"diarize_system": True, "echo_dedupe": True, "keep_audio": False})
    ctl.set_keep_audio(folder, True)
    assert folder.read_meta()["settings"] == {"diarize_system": True, "echo_dedupe": True, "keep_audio": True}
    ctl.set_keep_audio(folder, False)
    assert folder.read_meta()["settings"]["keep_audio"] is False


def test_set_keep_audio_is_honoured_by_the_final_pass_even_when_the_setting_is_off(ctl, tmp_path):
    folder = kept(tmp_path)
    folder.update_meta(settings={"keep_audio": False})
    ctl.set_keep_audio(folder, True)  # ticked while the meeting was processing
    finalize._remove_audio(folder, keep_audio=False)  # the pass was started with the old value
    assert folder.audio_file("mic").exists()
    ctl.set_keep_audio(folder, False)
    finalize._remove_audio(folder, keep_audio=False)
    assert not folder.audio_dir.exists()


def test_the_file_handle_is_not_left_open_so_the_folder_can_be_deleted_straight_after_a_play(ctl, tmp_path):
    import shutil

    folder = kept(tmp_path)
    ctl.play_clip(folder, clip())
    ctl.player.started.wait(5)
    shutil.rmtree(folder.path)  # no ignore_errors: fails on Windows if the clip's read left a handle open
    assert not folder.path.exists()


# -- Play from here ---------------------------------------------------------------------------------------


class RunFake:
    """A fake output for a real Player (gain 0.0): records writes, never opens a device."""

    def __init__(self):
        self.rate, self.channels, self.writes, self.closed = 16_000, 1, [], False

    def write(self, pcm):
        time.sleep(0.002)
        self.writes.append(pcm)

    def close(self):
        self.closed = True


@pytest.fixture()
def run_ctl(tmp_path):
    outs = []

    def factory():
        o = RunFake()
        outs.append(o)
        return o

    c = app.AppController(overrides={"meetings_dir": str(tmp_path)}, player=playback.Player(0.0, factory))
    c._jobs = queue.Queue()
    c.notes, c.outs, c.keys = [], outs, []
    c._notify = c.notes.append
    c._on_change = lambda: c.keys.append(c.playing_key)
    return c


def run_clips(*spans):
    return [playback.Clip(playback.line_key("system", a), ("system",), a, b, f"words {a}", 120) for a, b in spans]


def wait_idle(ctl, timeout=10):
    end = time.monotonic() + timeout
    while ctl.playing_key is not None and time.monotonic() < end:
        time.sleep(0.01)
    assert ctl.playing_key is None, "the run never ended"


def test_play_from_walks_the_highlight_through_the_clips_and_ends_idle(run_ctl, tmp_path):
    folder = kept(tmp_path)
    res = run_ctl.play_from(folder, run_clips((1000, 2000), (3000, 4000), (6000, 7000)))
    assert res.status == "playing" and run_ctl.playing_key == "line:system:1000" and run_ctl.playing_run
    wait_idle(run_ctl)
    seen = [k for i, k in enumerate(run_ctl.keys) if k and (i == 0 or run_ctl.keys[i - 1] != k)]
    assert seen == ["line:system:1000", "line:system:3000", "line:system:6000"]
    assert not run_ctl.playing_run and len(run_ctl.outs) == 1 and run_ctl.outs[0].closed  # one stream for the run
    assert not any(np.frombuffer(b"".join(run_ctl.outs[0].writes), dtype=np.int16))  # gain 0.0: silence


def test_stop_playback_ends_a_run_and_clicking_another_clip_replaces_it(run_ctl, tmp_path):
    folder = kept(tmp_path, seconds=40.0)
    run_ctl.play_from(folder, run_clips((1000, 9000), (10_000, 19_000), (20_000, 29_000)))
    time.sleep(0.2)
    run_ctl.stop_playback()
    assert run_ctl.playing_key is None and not run_ctl.playing_run and run_ctl.outs[0].closed
    run_ctl.play_from(folder, run_clips((1000, 9000), (10_000, 19_000)))
    time.sleep(0.1)
    assert run_ctl.play_clip(folder, clip(start=2000, end=3000)).status == "playing"
    assert not run_ctl.playing_run and run_ctl.playing_key == "line:system:2000"
    wait_idle(run_ctl)
    assert run_ctl.notes == []


def test_a_run_over_unreadable_audio_says_so_and_does_not_stay_busy(run_ctl, tmp_path):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_PROCESSING)  # no audio files
    run_ctl.play_from(folder, run_clips((1000, 2000)))
    wait_idle(run_ctl)
    assert run_ctl.notes == ["Could not read that audio."] and run_ctl.outs == []
    assert run_ctl.play_from(folder, []).status == "unavailable"


def test_while_recording_every_clip_of_a_run_opens_its_window_and_comes_from_the_ring(run_ctl, tmp_path):
    folder = recording(run_ctl, tmp_path)
    res = run_ctl.play_from(folder, run_clips((10_000, 12_000), (14_000, 16_000)))
    assert res.status == "playing" and res.notice == playback.HEADPHONES
    wait_idle(run_ctl)
    noted = run_ctl._recorder.noted
    assert [n[1:] for n in noted[:2]] == [("words 10000", "run"), ("words 14000", "run")] and noted[-1] == "run ended"
    assert noted[0][0] == pytest.approx(2.24, abs=0.05)  # 2 s + 120 ms of padding each side
    again = run_ctl.play_from(folder, run_clips((20_000, 21_000)))
    assert again.notice == ""  # the warning is once per meeting, not once per run
    wait_idle(run_ctl)


def test_a_run_while_recording_asks_once_when_echo_dedupe_has_seen_echo(run_ctl, tmp_path):
    folder = recording(run_ctl, tmp_path, echo=2)
    res = run_ctl.play_from(folder, run_clips((10_000, 12_000)))
    assert res.status == "confirm" and run_ctl._recorder.noted == [] and run_ctl.outs == []
    assert run_ctl.play_from(folder, run_clips((10_000, 12_000)), confirmed=True).status == "playing"
    wait_idle(run_ctl)


def test_lines_that_left_the_ring_during_a_run_are_skipped_not_played(run_ctl, tmp_path):
    folder = recording(run_ctl, tmp_path)
    run_ctl._recorder.ring = RecentAudio(10)
    run_ctl._recorder.ring.write(tone(300, 30.0))  # holds 20 s .. 30 s
    run_ctl.play_from(folder, run_clips((5_000, 6_000), (22_000, 23_000)))
    wait_idle(run_ctl)
    assert [n[1] for n in run_ctl._recorder.noted if isinstance(n, tuple)] == ["words 22000"]  # the old one is not played


def test_the_clip_device_setting_reaches_the_player_and_a_missing_device_is_told_once(run_ctl):
    run_ctl.update_settings(replace_settings(run_ctl, clip_output_device="Headset (USB)"))
    assert run_ctl.player.device_name == "Headset (USB)"
    run_ctl._clip_device_missing("Headset (USB)")
    run_ctl._clip_device_missing("Headset (USB)")
    assert len(run_ctl.notes) == 1 and "Windows default output" in run_ctl.notes[0]
    run_ctl.update_settings(replace_settings(run_ctl, clip_output_device=None))
    assert run_ctl.player.device_name is None


def replace_settings(ctl, **kw):
    from dataclasses import replace

    return replace(ctl.settings, **kw)
