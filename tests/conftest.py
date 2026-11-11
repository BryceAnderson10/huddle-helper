import numpy as np
import pytest

from huddle_helper import meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.diarize import Turn
from huddle_helper.finalize import append_words
from huddle_helper.segments import Segment, Word
from huddle_helper.transcribe import Transcription
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event


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


class ScriptedTranscriber:
    """Fake second-pass ASR: answers by the length of the audio it is given (ms) or with a default.

    Records every call as (ms, prompt); raises on ``fail_on`` ms; ``words=False`` returns no word timings.
    """

    name = "scripted"

    def __init__(self, default="new text", by_ms=None, fail_on=None, words=True):
        self.default, self.by_ms, self.fail_on, self.words, self.calls = default, by_ms or {}, fail_on, words, []

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> Transcription:
        ms = len(audio) * 1000 // SAMPLE_RATE
        self.calls.append((ms, prompt))
        if self.fail_on == ms:
            raise RuntimeError("boom")
        text = self.by_ms.get(ms, self.default)
        words = [Word(100 + 200 * i, 250 + 200 * i, " " + w) for i, w in enumerate(text.split())] if self.words else []
        return Transcription(text, 0.8, words)


class FakeDiarizer:
    """Fake diarizer: two turns, 0-2 s and 2-9 s, so the two system lines of
    ``make_meeting_to_finalize`` (at 0.5 s and 2.5 s) land on different speakers."""

    def diarize(self, audio, progress=None):
        if progress:
            progress(1.0)
        return [Turn(0, 2000, 5), Turn(2000, 9000, 2)]


def make_meeting_to_finalize(tmp_path, keep_audio=False):
    """A stopped meeting waiting to be finalized: 3 s of silent system audio, one mic and two system lines."""
    started = mt.now_local()
    folder = mt.MeetingFolder.create(tmp_path, "Test", started, {
        "settings": {"diarize_system": True, "diarize_mic": False, "keep_audio": keep_audio}})
    import soundfile as sf
    sf.write(str(folder.audio_file("system")), np.zeros(16000 * 3, np.float32), 16000, format="FLAC")
    segs = [Segment("mic", 100, 900, "Hello.", 0.9, "You"),
            Segment("system", 500, 1500, "First.", 0.9, "SPEAKER_01", [Word(500, 1500, " First.")]),
            Segment("system", 2500, 3000, "Second.", 0.9, "SPEAKER_01", [Word(2500, 3000, " Second.")])]
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, mt.iso(started)))
        for i, s in enumerate(segs, 1):
            w.write(s.to_event(i))
            append_words(folder.words_path, s)
        w.write(meeting_end_event(mt.iso(started), len(segs)))
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)
    return folder
