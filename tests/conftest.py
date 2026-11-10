import numpy as np
import pytest

from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.transcribe import Transcription


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HUDDLE_HELPER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HUDDLE_HELPER_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def tone(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


class ToneTranscriber:
    """Fake ASR: 'transcribes' a tone as the name of its frequency."""

    name = "fake-tone"

    def __init__(self):
        self.prompts: list[str | None] = []  # the prompt of every call, for tests

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> Transcription:
        self.prompts.append(prompt)
        if len(audio) == 0 or np.abs(audio).max() < 0.01:
            return Transcription("", 0.0)
        spectrum = np.abs(np.fft.rfft(audio))
        freq = int(round(np.argmax(spectrum) * SAMPLE_RATE / len(audio) / 50.0) * 50)
        return Transcription(f"Tone{freq}.", 0.9)


class ToneEmbedder:
    """Fake speaker fingerprint: one-hot by frequency band."""

    def embed(self, audio: np.ndarray):
        spectrum = np.abs(np.fft.rfft(audio))
        freq = np.argmax(spectrum) * SAMPLE_RATE / len(audio)
        vec = np.zeros(8, dtype=np.float32)
        vec[min(int(freq // 250), 7)] = 1.0
        return vec


def speechlike(seconds: float, amp: float = 0.3, hiss: float = 0.0, seed: int = 1) -> np.ndarray:
    """Tone bursts of 0.2 s with 0.1 s gaps (a level that rises and falls like speech), plus optional hiss."""
    x = tone(300, seconds, amp)
    on = (np.arange(len(x)) % int(0.3 * SAMPLE_RATE)) < int(0.2 * SAMPLE_RATE)
    x = x * on
    if hiss:
        x = x + np.random.default_rng(seed).normal(0, hiss, len(x)).astype(np.float32)
    return x.astype(np.float32)
