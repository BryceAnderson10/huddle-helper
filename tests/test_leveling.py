"""Quiet clips raised to a normal level for playback (leveling.py): target, cap, noise-only, limiter."""

import numpy as np
import pytest

from huddle_helper import leveling as lv

from .conftest import speechlike


def test_quiet_speech_is_raised_to_the_target_speech_level():
    x = speechlike(3.0, amp=0.03)  # about -33 dBFS speech level
    out = lv.level_info(x)
    assert lv.measure(out.audio).speech_rms == pytest.approx(lv.TARGET_RMS, rel=0.05)
    assert 1.0 < out.gain < 10 ** (lv.MAX_BOOST_DB / 20) and not out.capped and not out.limited


def test_a_clip_already_at_or_above_the_target_is_left_alone():
    x = speechlike(2.0, amp=0.5)
    out = lv.level_info(x)
    assert out.gain == 1.0 and np.array_equal(out.audio, x)


def test_the_boost_is_capped_at_24_db():
    x = speechlike(2.0, amp=0.005)  # speech at -51 dBFS: would need +31 dB
    out = lv.level_info(x)
    assert out.capped and out.gain_db == pytest.approx(lv.MAX_BOOST_DB, abs=0.01)
    assert lv.measure(out.audio).speech_db == pytest.approx(out.before.speech_db + lv.MAX_BOOST_DB, abs=0.1)  # still below the target: not blown up


def test_noise_alone_and_silence_are_not_raised():
    rng = np.random.default_rng(3)
    hiss = rng.normal(0, 0.004, 48_000).astype(np.float32)  # steady hiss: nothing stands out of it
    out = lv.level_info(hiss)
    assert out.gain == 1.0 and out.before.speech_rms == 0.0
    assert lv.level_info(np.zeros(16_000, np.float32)).gain == 1.0
    very_low = speechlike(2.0, amp=0.0015)  # below -54 dBFS even in the loud steps: treated as nothing
    assert lv.level_info(very_low).gain == 1.0


def test_a_hissy_clip_is_not_raised_into_a_hiss():
    x = speechlike(3.0, amp=0.02, hiss=0.004)  # floor about -48 dBFS
    out = lv.level_info(x)
    floor_after = lv.measure(out.audio).floor_rms
    assert floor_after <= lv.NOISE_CEIL * 1.15
    assert out.gain > 1.0


def test_the_limiter_never_exceeds_1_and_rounds_peaks_instead_of_flattening_them():
    x = speechlike(2.0, amp=0.05)
    x[8000] = 0.6  # one click: raising the speech by x2+ would push it over full scale
    out = lv.level_info(x)
    assert out.limited and float(np.abs(out.audio).max()) <= 1.0
    # untouched below the knee, monotonic above it: a bigger input is never a smaller output
    ramp = np.linspace(0, 3.0, 1000, dtype=np.float32)
    y, hit = lv.limit(ramp)
    assert hit and (np.diff(y) >= 0).all() and y.max() < 1.0 + 1e-6
    assert np.array_equal(y[ramp <= lv.KNEE], ramp[ramp <= lv.KNEE])
    assert len(np.unique(np.round(y[ramp > 1.2], 4))) > 5  # not a flat 1.0 plateau


def test_degenerate_inputs():
    assert len(lv.level_clip(np.zeros(0, np.float32))) == 0
    tiny = np.full(100, 0.01, np.float32)  # under three 20 ms steps: the whole clip is the level
    assert lv.level_info(tiny).audio.dtype == np.float32
