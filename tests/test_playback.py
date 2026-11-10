"""Click to hear: clips, the ring, the player (with a fake output device), the drop rule, the reasons."""

import shutil
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import meeting as mt, playback
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.audio.ring import RecentAudio
from huddle_helper.transcribe import MAX_GAIN, TARGET_PEAK

from .conftest import tone


def write_flac(path, samples, rate=SAMPLE_RATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, rate, format="FLAC", subtype="PCM_16")


def ramp(seconds, rate=SAMPLE_RATE, amp=0.4):
    """A signal whose value says where in time it is (so a clip's bounds can be read off the samples)."""
    return np.linspace(0.0, amp, int(seconds * rate), dtype=np.float32)


@pytest.fixture()
def folder(tmp_path):
    f = mt.MeetingFolder(tmp_path / "m")
    f.audio_dir.mkdir(parents=True)
    return f


# -- clip bounds ----------------------------------------------------------------------------------


def test_clip_range_pads_300_ms_and_stops_at_the_start_of_the_meeting():
    assert playback.clip_range(5000, 6000) == (4700, 6300)
    assert playback.clip_range(100, 900) == (0, 1200)


def test_clip_from_files_pads_both_sides(folder):
    write_flac(folder.audio_file("system"), ramp(10.0))
    clip = playback.clip_from_files(folder, "system", 5000, 6000)
    assert len(clip) == int(1.6 * SAMPLE_RATE)  # 300 + 1000 + 300 ms
    assert clip.dtype == np.float32


def test_clip_from_files_at_the_start_and_end_of_the_file(folder):
    write_flac(folder.audio_file("mic"), tone(300, 2.0))
    head = playback.clip_from_files(folder, "mic", 0, 500)
    assert len(head) == int(0.8 * SAMPLE_RATE)  # no 300 ms before the start of the file
    tail = playback.clip_from_files(folder, "mic", 1700, 2000)
    assert len(tail) == int(0.6 * SAMPLE_RATE)  # 300 ms before, the line, and nothing after the end
    assert playback.clip_from_files(folder, "mic", 5000, 6000) is None  # entirely past the end
    assert playback.clip_from_files(folder, "system", 0, 500) is None  # no such stream


def test_clip_from_files_resamples_a_file_that_is_not_16_khz(folder):
    write_flac(folder.audio_file("system"), np.sin(np.arange(96_000) / 20.0).astype(np.float32) * 0.3, 48_000)
    clip = playback.clip_from_files(folder, "system", 500, 1000)
    assert abs(len(clip) - int(1.1 * SAMPLE_RATE)) <= 2  # 300 ms + 500 ms + 300 ms


def test_the_file_handle_is_closed_after_a_read(folder, tmp_path):
    write_flac(folder.audio_file("mic"), tone(300, 2.0))
    write_flac(folder.audio_file("system"), tone(500, 2.0))
    playback.clip_from_files(folder, "mic", 100, 900)
    playback.mixed_from_files(folder, playback.Clip("mark:1", ("mic", "system"), 0, 1500, ""))
    shutil.rmtree(folder.path)  # raises on Windows if any handle is still open (no ignore_errors)
    assert not folder.path.exists()


def test_quiet_clips_are_raised_with_the_transcribers_gain_cap_and_loud_ones_left_alone(folder):
    write_flac(folder.audio_file("mic"), tone(300, 2.0, amp=0.04))
    write_flac(folder.audio_file("system"), tone(300, 2.0, amp=0.6))
    quiet = playback.clip_from_files(folder, "mic", 200, 1500)
    assert float(np.abs(quiet).max()) == pytest.approx(TARGET_PEAK, rel=0.02)  # 0.04 -> 0.5 is x12.5, under the cap
    loud = playback.clip_from_files(folder, "system", 200, 1500)
    assert float(np.abs(loud).max()) == pytest.approx(0.6, rel=0.02)
    write_flac(folder.audio_file("mic"), tone(300, 2.0, amp=0.001))  # needs x500: capped
    very_quiet = playback.clip_from_files(folder, "mic", 200, 1500)
    assert float(np.abs(very_quiet).max()) <= 0.001 * MAX_GAIN * 1.05


def test_mix_sums_streams_pads_the_shorter_and_never_clips():
    a, b = np.full(100, 0.4, np.float32), np.full(150, 0.4, np.float32)
    out = playback.mix([a, None, b])
    assert len(out) == 150 and out[0] == pytest.approx(0.8) and out[120] == pytest.approx(0.4)
    loud = playback.mix([np.full(10, 0.9, np.float32), np.full(10, 0.9, np.float32)])
    assert float(np.abs(loud).max()) == pytest.approx(1.0)
    assert playback.mix([None]) is None and playback.mix([a]) is a


# -- the ring ---------------------------------------------------------------------------------------


def test_ring_returns_what_was_written_and_wraps_around():
    ring = RecentAudio(seconds=2.0)
    data = np.linspace(-0.5, 0.5, 3 * SAMPLE_RATE, dtype=np.float32)  # 3 s into a 2 s ring
    for i in range(0, len(data), 1600):  # in 100 ms chunks, so it wraps part-way through a chunk boundary
        ring.write(data[i:i + 1600])
    assert ring.total_ms == 3000 and ring.oldest_ms == 1000
    got = ring.read(1500, 2500)
    np.testing.assert_allclose(got, data[1500 * 16:2500 * 16], atol=1 / 16000)
    np.testing.assert_allclose(ring.read(1000, 3000), data[1000 * 16:], atol=1 / 16000)


def test_ring_wraps_inside_a_single_chunk_and_keeps_the_tail_of_a_huge_one():
    ring = RecentAudio(seconds=1.0)
    ring.write(np.full(12_000, 0.1, np.float32))
    ring.write(np.full(8_000, 0.2, np.float32))  # crosses the end of the buffer
    got = ring.read(500, 1250)
    assert got[0] == pytest.approx(0.1, abs=1e-3) and got[-1] == pytest.approx(0.2, abs=1e-3)
    ring.write(np.full(40_000, 0.3, np.float32))  # bigger than the ring
    assert ring.read(1250, 2000) is None  # gone
    assert ring.read(ring.oldest_ms, ring.total_ms)[0] == pytest.approx(0.3, abs=1e-3)


def test_ring_refuses_ranges_outside_the_window_and_clamps_the_end():
    ring = RecentAudio(seconds=1.0)
    ring.write(np.full(32_000, 0.2, np.float32))  # 2 s into a 1 s ring
    assert ring.read(0, 500) is None  # left the window
    assert ring.read(999, 1500) is None  # starts a millisecond before the oldest audio held
    assert ring.read(5000, 6000) is None  # not written yet
    assert ring.read(1500, 1500) is None
    assert len(ring.read(1500, 9000)) == 8000  # the end is clamped to what exists
    assert RecentAudio(1.0).read(0, 100) is None  # empty ring


def test_clip_from_ring_pads_but_lets_the_padding_fall_outside_the_window():
    ring = RecentAudio(seconds=10.0)
    ring.write(ramp(12.0))  # holds 2 s .. 12 s
    assert playback.clip_from_ring(ring, 1000, 1500) is None  # the line itself has left the window
    clip = playback.clip_from_ring(ring, 2100, 3000)
    assert len(clip) == int((3300 - 2000) / 1000 * SAMPLE_RATE)  # padding before clamped to the oldest audio
    assert len(playback.clip_from_ring(ring, 11_500, 11_900)) == int(0.8 * SAMPLE_RATE)  # from 300 ms before to the head (12 s): the padding after is not there yet
    assert playback.clip_from_ring(None, 0, 100) is None


# -- the player (a fake output device: no sound card is opened) -----------------------------------------


class FakeOutput:
    def __init__(self, rate=48_000, channels=2, slow=0.0):
        self.rate, self.channels, self.slow = rate, channels, slow
        self.writes, self.closed = [], False

    def write(self, pcm):
        if self.slow:
            time.sleep(self.slow)
        self.writes.append(pcm)

    def close(self):
        self.closed = True


def fake_player(out=None, gain=1.0):
    outs = []

    def factory():
        o = out or FakeOutput()
        outs.append(o)
        return o

    return playback.Player(gain, factory), outs


def wait_done(done: threading.Event, timeout=5):
    assert done.wait(timeout), "the player never finished"


def test_player_resamples_to_the_device_rate_duplicates_mono_and_closes():
    player, outs = fake_player()
    done, reasons = threading.Event(), []
    player.play(tone(440, 1.0, amp=0.3), lambda r: (reasons.append(r), done.set()))
    wait_done(done)
    out = outs[0]
    pcm = np.frombuffer(b"".join(out.writes), dtype=np.int16).reshape(-1, 2)
    assert abs(len(pcm) - 48_000) < 100  # 1 s at 48 kHz
    assert (pcm[:, 0] == pcm[:, 1]).all()  # mono on both channels
    assert reasons == ["finished"] and out.closed
    assert 0.25 * 32767 < np.abs(pcm).max() < 0.31 * 32767


def test_player_output_gain_scales_and_is_capped_at_1():
    assert playback.Player().gain == 1.0  # the app plays at full level
    assert playback.Player(gain=5.0).gain == 1.0 and playback.Player(gain=-1).gain == 0.0
    frames = playback.prepare_pcm(tone(440, 0.5, amp=0.5), 16_000, 1, gain=0.02)
    assert np.abs(frames).max() <= 0.5 * 0.02 * 32767 + 1
    loud = playback.prepare_pcm(np.full(100, 2.0, np.float32), 16_000, 2, gain=1.0)
    assert loud.max() == 32767 and loud.shape == (100, 2)  # never wraps around


def test_player_plays_one_clip_at_a_time_and_stop_waits_for_the_device_to_close():
    outs = []

    def factory():
        o = FakeOutput(slow=0.01)
        outs.append(o)
        return o

    player = playback.Player(0.02, factory)
    reasons = []
    player.play(tone(440, 5.0), lambda r: reasons.append(("a", r)))
    time.sleep(0.15)
    assert player.playing
    done = threading.Event()
    player.play(tone(440, 0.2), lambda r: (reasons.append(("b", r)), done.set()))  # stops the first
    wait_done(done)
    assert reasons == [("a", "stopped"), ("b", "finished")]
    assert all(o.closed for o in outs) and len(outs) == 2
    player.stop()  # idle: harmless
    player.play(tone(440, 5.0))
    time.sleep(0.1)
    player.stop()
    assert not player.playing and outs[-1].closed  # closed by the time stop() returns


def test_player_reports_a_device_error_and_does_not_stay_busy():
    def broken():
        raise OSError("no output device")

    player = playback.Player(0.02, broken)
    done, reasons = threading.Event(), []
    player.play(tone(440, 0.2), lambda r: (reasons.append(r), done.set()))
    wait_done(done)
    assert reasons[0].startswith("error") and "no output device" in reasons[0]
    time.sleep(0.05)
    assert not player.playing


# -- the drop rule -------------------------------------------------------------------------------------


def test_an_utterance_that_overlaps_a_window_and_repeats_its_words_is_dropped():
    w = playback.PlaybackWindows()
    w.add(10_000, 3.0, "We can ship the release on Friday.")
    assert w.match(10_800, 13_400, "We can ship the release on Friday.") is True
    assert w.match(10_800, 13_400, "We can ship the release on Friday") is True
    assert w.match(10_800, 13_400, "ship the release on Friday") is True  # 5 of 5 words covered
    assert w.snapshot() == (1, 3)


def test_overlap_without_a_text_match_is_kept():
    w = playback.PlaybackWindows()
    w.add(10_000, 3.0, "We can ship the release on Friday.")
    assert w.match(11_000, 13_000, "Legal still needs one more day before anything goes out.") is False
    assert w.match(11_000, 13_000, "Yeah sounds good.") is False
    assert w.snapshot() == (1, 0)


def test_a_text_match_outside_every_window_is_kept():
    w = playback.PlaybackWindows()
    w.add(10_000, 3.0, "We can ship the release on Friday.")
    assert w.match(60_000, 63_000, "We can ship the release on Friday.") is False  # said again, much later
    assert w.match(0, 2_000, "We can ship the release on Friday.") is False  # and before
    assert w.match(10_000, 12_000, "") is False


def test_short_clips_need_every_word_to_match():
    w = playback.PlaybackWindows()
    w.add(5_000, 1.0, "Sounds good")
    assert w.match(5_200, 6_200, "Sounds good") is True
    assert w.match(5_200, 6_200, "Sounds great") is False  # one of two: not enough for short text


def test_windows_are_thread_safe_to_add_while_matching():
    w = playback.PlaybackWindows()
    stop = threading.Event()

    def matcher():
        while not stop.is_set():
            w.match(0, 10_000_000, "a b c d e f")

    t = threading.Thread(target=matcher)
    t.start()
    for i in range(300):
        w.add(i * 1000, 1.0, "a b c d e f")
    stop.set()
    t.join()
    assert w.count == 300


# -- headphones --------------------------------------------------------------------------------------------


def test_headphone_warning_once_per_meeting_and_confirmation_after_echo():
    assert playback.headphone_check(False, False, False, 5).action == "play"  # not recording: nothing to warn about
    first = playback.headphone_check(True, False, False, 0)
    assert first.action == "warn" and "Use headphones" in first.message
    assert playback.headphone_check(True, True, False, 0).action == "play"  # once per meeting
    ask = playback.headphone_check(True, False, False, 2)
    assert ask.action == "confirm" and "Play it anyway?" in ask.message
    assert playback.headphone_check(True, True, False, 2).action == "confirm"  # warned, but echo showed up since
    assert playback.headphone_check(True, True, True, 2).action == "play"  # confirmed once: no more asking
    assert playback.headphone_check(True, False, True, 2).action == "warn"  # still gets the plain warning once


# -- what to play ------------------------------------------------------------------------------------------


def line(source, start, end, key="A", conf=0.9, text="some words"):
    return SimpleNamespace(source=source, start_ms=start, end_ms=end, person_key=key, confidence=conf, text=text)


def test_bookmark_range_is_10_seconds_before_to_5_after_and_not_before_zero():
    assert playback.bookmark_range(60_000) == (50_000, 65_000)
    assert playback.bookmark_range(4_000) == (0, 9_000)


def test_bookmark_clip_plays_both_streams_and_collects_the_text_in_range():
    rows = [line("mic", 52_000, 54_000, text="first"), line("system", 58_000, 60_000, text="second"),
            line("system", 80_000, 81_000, text="far later"), line("system", 40_000, 41_000, text="far earlier")]
    mark = SimpleNamespace(id=7, at_ms=60_000)
    clip = playback.bookmark_clip(rows, mark)
    assert (clip.start_ms, clip.end_ms, clip.sources, clip.key) == (50_000, 65_000, ("mic", "system"), "mark:7")
    assert clip.text == "first second"


def test_hear_this_voice_prefers_the_longest_confident_line_of_3_to_8_seconds():
    rows = [line("system", 0, 1_500, conf=0.95),          # too short
            line("system", 10_000, 15_000, conf=0.9),     # 5 s
            line("system", 20_000, 26_500, conf=0.8),     # 6.5 s  <- longest in range
            line("system", 30_000, 41_000, conf=0.99),    # 11 s: longer, but out of range
            line("system", 50_000, 57_000, conf=0.4),     # in range but not confident
            line("mic", 60_000, 66_000, key="B")]
    assert playback.pick_voice_line(rows, "A").start_ms == 20_000


def test_hear_this_voice_falls_back_to_the_longest_line_and_to_low_confidence():
    rows = [line("system", 0, 1_500), line("system", 10_000, 12_500), line("system", 30_000, 41_000)]
    assert playback.pick_voice_line(rows, "A").start_ms == 30_000  # nothing of 3-8 s: the longest
    only_unsure = [line("system", 0, 4_000, conf=0.3), line("system", 10_000, 15_000, conf=0.5)]
    assert playback.pick_voice_line(only_unsure, "A").start_ms == 10_000
    assert playback.pick_voice_line(rows, "nobody") is None
    assert playback.pick_voice_line([line("system", 0, 4000, text="  ")], "A") is None


def test_hear_this_voice_only_considers_lines_whose_audio_can_be_read():
    rows = [line("system", 100_000, 105_000), line("system", 700_000, 705_000)]
    ctx = playback.AudioContext("ring", oldest_ms=(("system", 600_000),))
    pick = playback.pick_voice_line(rows, "A", lambda r: ctx.state(r.source, r.start_ms).enabled)
    assert pick.start_ms == 700_000


# -- disabled controls and their reasons -------------------------------------------------------------------------


def ctx_for(**kw):
    base = dict(recording=False, stopping=False, done=False, meta={}, present=[], oldest_ms=None)
    base.update(kw)
    return playback.audio_context(**base)


def test_while_recording_lines_play_from_the_ring_and_older_ones_wait_for_stop():
    ctx = ctx_for(recording=True, oldest_ms={"mic": 0, "system": 120_000})
    assert ctx.state("mic", 5_000).enabled
    assert ctx.state("system", 130_000).enabled
    old = ctx.state("system", 60_000)
    assert not old.enabled and old.reason == playback.AFTER_STOP and "Available after Stop" in old.reason
    assert ctx.state("nope", 0).reason == playback.NO_STREAM


def test_between_stop_and_done_the_files_are_used_and_a_missing_stream_says_so():
    ctx = ctx_for(present=["system"])
    assert ctx.mode == "files" and ctx.state("system", 5).enabled
    assert ctx.state("mic", 5).reason == playback.NO_STREAM
    stopping = ctx_for(stopping=True)
    assert not stopping.state("mic", 0).enabled and stopping.state("mic", 0).reason == playback.NOT_READABLE


def test_after_done_without_audio_the_reason_is_the_one_in_live_view():
    ctx = ctx_for(done=True)
    assert ctx.state("mic", 0).reason == playback.DELETED_AFTER_FINAL
    assert "Turn on Keep audio to hear lines later" in playback.DELETED_AFTER_FINAL
    assert ctx_for(done=True, meta={"audio_deleted_at": "x"}).state("mic", 0).reason == playback.DELETED_BY_USER
    assert ctx_for(done=True, meta={"audio_purged_at": "x"}).state("mic", 0).reason == playback.PURGED
    assert ctx_for(done=False).state("mic", 0).reason == playback.NO_AUDIO


def test_a_bookmark_plays_if_any_stream_has_audio_in_its_span():
    clip = playback.Clip("mark:1", ("mic", "system"), 50_000, 65_000, "")
    ring = ctx_for(recording=True, oldest_ms={"mic": 62_000, "system": 90_000})
    assert ring.clip_state(clip).enabled  # the mic ring still reaches the end of the span
    gone = ctx_for(recording=True, oldest_ms={"mic": 70_000, "system": 90_000})
    assert not gone.clip_state(clip).enabled and gone.clip_state(clip).reason == playback.AFTER_STOP


def test_the_context_signature_changes_only_when_the_look_of_the_controls_can_change():
    a = ctx_for(recording=True, oldest_ms={"mic": 1_000, "system": 1_000})
    b = ctx_for(recording=True, oldest_ms={"mic": 1_500, "system": 1_900})
    c = ctx_for(recording=True, oldest_ms={"mic": 12_000, "system": 1_000})
    assert a.signature == b.signature and a.signature != c.signature


# -- quiet points: the clip does not cut the last word or start on the previous speaker --------------------------------


def hush(seconds, level=0.0005):
    return np.full(int(seconds * SAMPLE_RATE), level, np.float32)


def cat(*parts):
    return np.concatenate(parts).astype(np.float32)


def test_the_end_runs_on_to_a_quiet_point_when_speech_continues_past_the_padding():
    # line 1.0-2.0 s; the last word's sound really lasts until 2.7 s; quiet after
    x = cat(hush(1.0), tone(300, 1.7), hush(1.0))
    y, y_ms = playback.cut_line(x, 0, 1000, 2000)
    assert y_ms == 700
    end_ms = y_ms + len(y) * 1000 / SAMPLE_RATE
    assert 2700 <= end_ms <= 2760  # to the first quiet 20 ms step, not the padded 2300


def test_the_end_stays_at_the_padding_when_there_is_no_quiet_point_in_reach():
    x = cat(hush(1.0), tone(300, 4.0))  # the next speaker follows at once
    y, y_ms = playback.cut_line(x, 0, 1000, 2000)
    assert y_ms + len(y) * 1000 / SAMPLE_RATE == 2300
    y, y_ms = playback.cut_line(cat(hush(1.0), tone(300, 1.0), hush(2.0)), 0, 1000, 2000)  # already quiet
    assert y_ms == 700 and len(y) == int(1.6 * SAMPLE_RATE)


def test_the_start_skips_the_previous_speakers_last_sounds_when_there_is_a_quiet_gap():
    x = cat(tone(500, 0.85), hush(0.2), tone(300, 1.0), hush(1.0))  # previous voice to 0.85 s, line 1.05-2.05 s
    y, y_ms = playback.cut_line(x, 0, 1050, 2050)
    assert 850 <= y_ms <= 1010  # starts in the gap, not at the padded 750 ms (inside the previous voice)


def test_the_start_goes_back_when_the_line_began_earlier_than_whisper_said():
    x = cat(hush(0.5), tone(300, 2.0), hush(1.0))  # speech from 0.5 s, but the line is said to start at 1.0 s
    y, y_ms = playback.cut_line(x, 0, 1000, 2000)
    assert 440 <= y_ms <= 500  # padded start 700 ms is loud: back to the quiet before 500 ms


def test_no_snapping_in_continuous_speech_or_in_a_very_short_line():
    x = tone(300, 5.0)
    y, y_ms = playback.cut_line(x, 0, 2000, 3000)
    assert (y_ms, len(y)) == (1700, int(1.6 * SAMPLE_RATE))
    y, y_ms = playback.cut_line(cat(hush(1.0), tone(300, 0.01), hush(1.0)), 0, 1000, 1010)
    assert y_ms == 700


def test_the_clip_ends_with_a_short_fade_so_a_cut_does_not_click(folder):
    write_flac(folder.audio_file("mic"), tone(300, 6.0, amp=0.5))
    clip = playback.clip_from_files(folder, "mic", 2000, 3000)
    assert abs(float(clip[-1])) < 0.02 and float(np.abs(clip[-1000:-400]).max()) > 0.4
