"""Quiet clips raised to a normal level, for playback only (LIVE_VIEW L19). Pure numpy, no device.

Why not ``transcribe.normalize_level``: that rule aims a *peak* at 0.5 with up to +30 dB, which is right
for Whisper but wrong for ears: one click in a quiet clip sets the peak and the speech stays quiet, and
a clip of hiss alone is blown up to a roar. Here the clip's *speech level* is measured and the clip is
moved to a target:

* **Speech level** = rms over the "active" 20 ms steps (steps clearly above the clip's own noise floor
  and within 12 dB of its loud steps), so the pauses inside a clip do not drag the level down.
* **Target**  ``TARGET_RMS`` = 0.10 (-20 dBFS), where ordinary speech sits in a normal recording.
  Louder clips are left alone (the rule only raises, never lowers).
* **Cap**  ``MAX_BOOST_DB`` = +24 dB (x15.8): Test #2's microphone speaks at about -42 dBFS and needs
  +22 dB; past +24 dB the noise floor is raised as much as the voice. A second cap keeps the *boosted
  noise floor* under ``NOISE_CEIL`` (-30 dBFS), so a hissy mic is not raised into a hiss.
* **Noise only** (no step stands ``MIN_SNR_DB`` above the floor, or the clip is below ``MIN_SPEECH_RMS``
  = -54 dBFS): not raised at all.
* **Limiter**: after the gain, anything above ``KNEE`` (0.89) is bent toward 1.0 with a tanh curve, so
  the output never exceeds 1.0 and a peak is rounded, not flattened.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE

TARGET_RMS = 0.10
MAX_BOOST_DB = 24.0
NOISE_CEIL = 0.03
MIN_SPEECH_RMS = 0.002
MIN_SNR_DB = 6.0
KNEE = 0.89
FRAME_S = 0.02
ACTIVE_BELOW_LOUD_DB = 12.0


def db(x: float) -> float:
    return 20.0 * float(np.log10(max(x, 1e-9)))


@dataclass(frozen=True)
class Level:
    speech_rms: float  # 0.0 when the clip is noise only or silent
    floor_rms: float
    peak: float

    @property
    def speech_db(self) -> float:
        return db(self.speech_rms)


@dataclass(frozen=True)
class Leveled:
    audio: np.ndarray
    gain: float
    before: Level
    capped: bool  # the boost wanted was cut by the +24 dB cap or the noise ceiling
    limited: bool  # the limiter bent at least one sample

    @property
    def gain_db(self) -> float:
        return db(self.gain)


def measure(audio: np.ndarray, rate: int = SAMPLE_RATE) -> Level:
    """Speech level, noise floor and peak of a mono clip."""
    x = np.asarray(audio, dtype=np.float32)
    if not len(x):
        return Level(0.0, 0.0, 0.0)
    peak = float(np.abs(x).max())
    n = max(1, int(rate * FRAME_S))
    k = len(x) // n
    if k < 3:  # too short for steps: the whole clip is the level
        r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        return Level(r, 0.0, peak)
    frames = np.sqrt(np.mean(x[:k * n].astype(np.float64).reshape(k, n) ** 2, axis=1))
    floor = float(np.percentile(frames, 10))
    loud = float(np.percentile(frames, 90))
    if loud < MIN_SPEECH_RMS or loud < floor * 10 ** (MIN_SNR_DB / 20.0):
        return Level(0.0, floor, peak)  # silence, or nothing stands out from the noise
    cut = max(loud * 10 ** (-ACTIVE_BELOW_LOUD_DB / 20.0), floor * 10 ** (MIN_SNR_DB / 40.0))
    active = frames[frames >= cut]
    speech = float(np.sqrt(np.mean(active ** 2))) if len(active) else loud
    return Level(speech, floor, peak)


def limit(x: np.ndarray, knee: float = KNEE) -> tuple[np.ndarray, bool]:
    """Soft limiter: unchanged up to ``knee``, above it bent toward (never reaching past) 1.0."""
    a = np.abs(x)
    if not len(x) or float(a.max()) <= knee:
        return x, False
    over = a > knee
    out = x.copy()
    out[over] = np.sign(x[over]) * (knee + (1.0 - knee) * np.tanh((a[over] - knee) / (1.0 - knee)))
    return np.clip(out, -1.0, 1.0), True


def level_info(audio: np.ndarray, rate: int = SAMPLE_RATE) -> Leveled:
    x = np.asarray(audio, dtype=np.float32)
    before = measure(x, rate)
    if before.speech_rms <= 0.0 or before.speech_rms >= TARGET_RMS:
        gain, capped = 1.0, False
    else:
        want = TARGET_RMS / before.speech_rms
        gain = min(want, 10 ** (MAX_BOOST_DB / 20.0))
        if before.floor_rms > 0:
            gain = min(gain, max(1.0, NOISE_CEIL / before.floor_rms))
        capped = gain < want - 1e-6
    y = x * np.float32(gain) if gain != 1.0 else x
    y, limited = limit(y)
    return Leveled(np.ascontiguousarray(y, dtype=np.float32), gain, before, capped, limited)


def level_clip(audio: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """``audio`` raised toward the target speech level; see the module notes. Never louder than 1.0."""
    return level_info(audio, rate).audio
