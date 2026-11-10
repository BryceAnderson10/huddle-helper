"""Play from here: which clips, the pauses, the run on a fake output, the player's run, the joined drop window."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from huddle_helper import playback, playsequence as ps
from huddle_helper.audio import SAMPLE_RATE

from .conftest import tone
from .test_playback import FakeOutput


def line(source, start, end, text="some words here", person="P1"):
    return SimpleNamespace(source=source, start_ms=start, end_ms=end, text=text, person_key=person)


ROWS = [line("mic", 1000, 2000), SimpleNamespace(kind="gap"), line("system", 5000, 7000),
        line("system", 7200, 8000, text="  "), line("mic", 20_000, 21_000), line("system", 30_000, 31_000)]


def test_plan_run_is_the_line_and_the_following_lines_in_order_each_on_its_own_stream():
    clips = ps.plan_run(ROWS, playback.line_key("system", 5000))
    assert [c.key for c in clips] == ["line:system:5000", "line:mic:20000", "line:system:30000"]  # the blank one is out
    assert [c.sources for c in clips] == [("system",), ("mic",), ("system",)]
    assert all(c.pad_ms == ps.RUN_PAD_MS for c in clips)
    assert ps.plan_run(ROWS, playback.line_key("mic", 999)) == []  # no such line


def test_plan_run_leaves_out_lines_whose_audio_is_not_available():
    clips = ps.plan_run(ROWS, playback.line_key("mic", 1000), can_play=lambda r: r.start_ms >= 5000)
    assert [c.key for c in clips] == ["line:system:5000", "line:mic:20000", "line:system:30000"]


def test_pause_is_the_gap_minus_the_padding_and_never_longer_than_400_ms():
    assert ps.pause_ms(1000, 1100) == 0 and ps.pause_ms(1000, 900) == 0  # touching or overlapping lines
    assert ps.pause_ms(1000, 1500) == 500 - 2 * ps.RUN_PAD_MS
    assert ps.pause_ms(1000, 1000 + 120_000) == ps.MAX_PAUSE_MS  # two minutes of silence play as 0.4 s


def clip(start, end, text="words"):
    return playback.Clip(playback.line_key("system", start), ("system",), start, end, text, ps.RUN_PAD_MS)


def run(clips, load, out=None, cancel=None, on_clip=None, gain=1.0):
    out = out or FakeOutput(rate=16_000, channels=1)
    opened = []

    def open_out():
        opened.append(1)
        return out

    res = ps.run_sequence(open_out, clips, load, cancel or threading.Event(), gain, on_clip)
    return res, out, opened


def pcm(out):
    return np.frombuffer(b"".join(out.writes), dtype=np.int16)


def test_a_run_writes_clips_with_pauses_between_on_one_stream_and_closes_it():
    clips = [clip(0, 1000), clip(1500, 2500), clip(120_000, 121_000)]
    seen, out = [], FakeOutput(rate=16_000, channels=1)
    res, out, opened = run(clips, lambda c: tone(300, 1.0, amp=0.4), out=out,
                           on_clip=lambda c, s: seen.append((c.key, len(out.writes))))
    n = len(pcm(out))
    pauses = ps.pause_ms(1000, 1500) + ps.pause_ms(2500, 120_000)  # 260 + 400
    assert n == 3 * SAMPLE_RATE + SAMPLE_RATE * pauses // 1000
    assert res == ps.RunResult("finished", 3, 0) and out.closed and opened == [1]
    assert [k for k, _ in seen] == [c.key for c in clips]
    assert seen[0][1] == 0 and seen[1][1] > 0  # on_clip comes before that clip's sound (and after the pause)
    assert np.abs(pcm(out)).max() <= 0.4 * 32767 + 2


def test_unreadable_clips_are_skipped_and_a_run_with_none_never_opens_the_device():
    clips = [clip(0, 1000), clip(2000, 3000), clip(4000, 5000)]
    res, out, _ = run(clips, lambda c: None if c.start_ms == 2000 else tone(300, 0.5))
    assert res == ps.RunResult("finished", 2, 1) and out.closed
    res, out, opened = run(clips, lambda c: None)
    assert res.reason == "unavailable" and opened == [] and out.writes == []


def test_cancel_stops_within_a_block_and_closes_the_device():
    cancel = threading.Event()
    out = FakeOutput(rate=16_000, channels=1)
    orig = out.write
    out.write = lambda b: (orig(b), cancel.set())  # the user presses Stop during the first block
    res, out, _ = run([clip(0, 1000), clip(2000, 3000)], lambda c: tone(300, 5.0), out=out, cancel=cancel)
    assert res.reason == "stopped" and res.played == 1 and out.closed and len(out.writes) == 1


def test_a_device_error_propagates_after_the_device_is_closed():
    out = FakeOutput(rate=16_000, channels=1)

    def boom(b):
        raise OSError("device lost")

    out.write = boom
    with pytest.raises(OSError):
        run([clip(0, 1000)], lambda c: tone(300, 1.0), out=out)
    assert out.closed


def test_gain_zero_writes_only_silence_whatever_the_levelling_did():
    res, out, _ = run([clip(0, 1000), clip(1500, 2500)], lambda c: np.full(16_000, 0.9, np.float32), gain=0.0)
    assert res.played == 2 and not pcm(out).any()


# -- the player's run ----------------------------------------------------------------------------------------


def test_player_play_run_plays_the_clips_and_reports_how_it_ended():
    outs = []

    def factory():
        o = FakeOutput(rate=16_000, channels=1)
        outs.append(o)
        return o

    player = playback.Player(0.0, factory)
    done, reasons, seen = threading.Event(), [], []
    player.play_run([clip(0, 500), clip(1000, 1500)], lambda c: tone(300, 0.5), lambda c, s: seen.append(c.key),
                    lambda r: (reasons.append(r), done.set()))
    assert done.wait(5)
    assert reasons == ["finished"] and len(seen) == 2 and len(outs) == 1 and outs[0].closed and not pcm(outs[0]).any()
    done.clear()
    player.play_run([clip(0, 500)], lambda c: None, None, lambda r: (reasons.append(r), done.set()))
    assert done.wait(5) and reasons[-1] == "unavailable"


def test_stop_ends_a_run_and_a_new_play_replaces_it():
    outs = []

    def factory():
        o = FakeOutput(rate=16_000, channels=1, slow=0.01)
        outs.append(o)
        return o

    player = playback.Player(0.0, factory)
    reasons = []
    player.play_run([clip(0, 5000), clip(6000, 9000)], lambda c: tone(300, 5.0), None, lambda r: reasons.append(("run", r)))
    time.sleep(0.15)
    assert player.playing
    player.stop()
    assert reasons == [("run", "stopped")] and not player.playing and outs[0].closed
    player.play_run([clip(0, 5000)], lambda c: tone(300, 5.0), None, lambda r: reasons.append(("a", r)))
    time.sleep(0.1)
    player.play(tone(300, 0.1), lambda r: reasons.append(("b", r)))  # a single clip takes over
    time.sleep(0.5)
    assert ("a", "stopped") in reasons and reasons[-1] == ("b", "finished")


# -- the drop rule over a run -------------------------------------------------------------------------------


def test_clips_a_few_hundred_ms_apart_that_come_back_as_one_utterance_are_still_dropped():
    first, second = "we ship the release on friday", "after the review is done on thursday"
    heard = f"{first} {second}"
    single = playback.PlaybackWindows()
    single.add(10_000, 3.0, first)
    single.add(13_300, 3.0, second)
    assert single.match(10_500, 17_000, heard) is False  # each clip alone covers under 60% of what was heard
    joined = playback.PlaybackWindows()
    joined.add_run_clip(10_000, 3.0, first)
    joined.add_run_clip(13_300, 3.0, second)
    assert joined.match(10_500, 17_000, heard) is True
    assert joined.snapshot() == (2, 1)  # two clips, one utterance dropped; the joined window is not a clip


def test_a_remote_person_talking_over_a_run_is_kept_and_the_join_forgets_old_clips():
    w = playback.PlaybackWindows()
    w.add_run_clip(10_000, 3.0, "we ship the release on friday")
    w.add_run_clip(13_300, 3.0, "after the review is done on thursday")
    assert w.match(11_000, 14_000, "can everyone see my screen right now") is False
    w.add_run_clip(60_000, 3.0, "something else entirely now")  # more than 12 s later: the first two are out of the join
    assert len(w._run) == 1  # only the newest clip is within 12 s of itself
    w.end_run()
    assert w._run == []
