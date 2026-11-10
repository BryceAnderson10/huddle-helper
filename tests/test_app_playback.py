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
        return playback.clip_from_ring(self.ring, clip.start_ms, clip.end_ms)

    def note_playback(self, duration_s, text):
        self.noted.append((round(duration_s, 2), text))
        return True

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
