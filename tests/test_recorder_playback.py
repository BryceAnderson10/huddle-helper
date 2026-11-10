"""Click to hear through the real Recorder (fake ASR, hand-fed audio): the ring behind the mic gate, the drop
rule on a clip that comes back through the system stream, and the capture's before_reopen call."""

import threading
import time

import numpy as np
import pytest

from huddle_helper import config, playback
from huddle_helper.audio import SAMPLE_RATE, capture as capture_mod
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone
from .test_mute import ManualCapture


class GatedTranscriber(ToneTranscriber):
    """Holds every transcription until the test has set up its playback windows."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()

    def transcribe(self, audio, prompt=None):
        assert self.go.wait(10)
        return super().transcribe(audio, prompt)


def start(tmp_path, transcriber=None):
    holder = {}

    def factory(**kw):
        holder["cap"] = ManualCapture(kw["on_audio"])
        holder["kw"] = kw
        return holder["cap"]

    comps = Components(transcriber=transcriber or ToneTranscriber(), embedder=ToneEmbedder(),
                       scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "meetings")), comps)
    folder = rec.start("Playback Test")
    return rec, folder, holder["cap"], holder


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def lines(folder):
    return [e for e in read_events(folder.live_path) if e.get("type") == "segment"]


# -- the ring holds gated samples only ----------------------------------------------------------------


def test_the_ring_holds_the_gated_mic_so_muted_speech_cannot_be_played_back(tmp_path):
    rec, folder, cap, _ = start(tmp_path)
    cap.push(tone(300, 2.0), silence(2.0))  # 0-2 s: speech, mic open
    rec.set_mic_muted(True)
    cap.push(tone(300, 2.0), silence(2.0))  # 2-4 s: speech while muted
    rec.set_mic_muted(False)
    cap.push(silence(1.0), silence(1.0))  # the 250 ms tail runs out in here
    cap.push(tone(300, 1.0), silence(1.0))  # 5-6 s: speech again, mic open
    ring = rec._workers["mic"].ring
    wait_for(lambda: ring.total_ms >= 6000)

    assert float(np.abs(ring.read(100, 1900)).max()) > 0.25  # what was said before the press
    muted = ring.read(2000, 4000)
    assert float(np.abs(muted).max()) == 0.0  # exactly zero: nothing to hear
    assert float(np.abs(ring.read(5100, 5900)).max()) > 0.25
    # and the same through the call the controller makes, with the padding of a line spoken while muted
    clip = playback.Clip("line:mic:2500", ("mic",), 2500, 3500, "muted words")
    played = rec.recent_clip(clip)
    assert played is not None and float(np.abs(played).max()) == 0.0
    # the bookmark mix of both streams has the system audio only
    mixed = rec.recent_clip(playback.Clip("mark:1", ("mic", "system"), 2000, 4000, ""))
    assert mixed is None or float(np.abs(mixed).max()) == 0.0
    rec.stop()


def test_the_ring_and_the_flac_buffer_agree(tmp_path):
    from huddle_helper.audio.buffer import read_audio

    rec, folder, cap, _ = start(tmp_path)
    cap.push(tone(440, 1.5), tone(880, 1.5, amp=0.2))
    ring = rec._workers["system"].ring
    wait_for(lambda: ring.total_ms >= 1500)
    from_ring = ring.read(0, 1500)
    rec.stop()
    from_file = read_audio(folder.audio_file("system"))
    assert len(from_file) == len(from_ring)
    np.testing.assert_allclose(from_ring, from_file, atol=2e-4)


def test_recent_oldest_ms_and_the_older_than_the_ring_case(tmp_path, monkeypatch):
    monkeypatch.setattr(playback, "RING_SECONDS", 2)
    rec, folder, cap, _ = start(tmp_path)
    cap.push(tone(300, 5.0), silence(5.0))
    wait_for(lambda: rec._workers["mic"].ring.total_ms >= 5000)
    assert rec.recent_oldest_ms() == {"mic": 3000, "system": 3000}
    assert rec.recent_clip(playback.Clip("line:mic:1000", ("mic",), 1000, 2000, "")) is None  # after Stop only
    assert rec.recent_clip(playback.Clip("line:mic:3500", ("mic",), 3500, 4500, "")) is not None
    rec.stop()


# -- the drop rule, with the clip audio fed back into the system stream ------------------------------------


def speech_then_replay(tmp_path, window_text, window_at_ms=6000, with_window=True):
    """System stream: remote speech (1100 Hz) at 1 s; the 'played clip' (1600 Hz, text Tone1600.) heard again
    at 6 s through the loopback; a different remote voice (2000 Hz) at 10 s while the window is still open."""
    tr = GatedTranscriber()
    rec, folder, cap, _ = start(tmp_path, tr)
    cap.push(silence(1.0), silence(1.0))
    cap.push(silence(2.0), tone(1100, 2.0))
    cap.push(silence(3.0), silence(3.0))
    cap.push(silence(2.0), tone(1600, 2.0))  # 6-8 s: the clip comes back through the loopback
    cap.push(silence(2.0), silence(2.0))
    cap.push(silence(2.0), tone(2000, 2.0))  # 10-12 s
    cap.push(silence(2.0), silence(2.0))
    if with_window:
        rec.playbacks.add(window_at_ms, 2.0, window_text)
    tr.go.set()
    rec.stop()
    return rec, folder


def test_a_clip_heard_again_is_dropped_and_founds_no_speaker(tmp_path):
    rec, folder = speech_then_replay(tmp_path, "Tone1600.")
    segs = lines(folder)
    assert [s["text"] for s in segs] == ["Tone1100.", "Tone2000."]  # the replayed clip is not a line
    assert [s["speaker"] for s in segs] == ["SPEAKER_01", "SPEAKER_02"]  # and did not take a speaker number
    meta = folder.read_meta()
    assert meta["playbacks"] == 1 and meta["playback_segments_dropped"] == 1
    assert meta["segments"] == 2


def test_without_a_playback_the_same_audio_gives_three_lines_and_three_speakers(tmp_path):
    rec, folder = speech_then_replay(tmp_path, "", with_window=False)
    segs = lines(folder)
    assert [s["speaker"] for s in segs] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]
    meta = folder.read_meta()
    assert meta["playbacks"] == 0 and meta["playback_segments_dropped"] == 0


def test_overlap_without_a_text_match_is_kept_through_the_recorder(tmp_path):
    # The window is open over the 1600 Hz and 2000 Hz speech, but it is a clip with other words.
    rec, folder = speech_then_replay(tmp_path, "Something else entirely was said here.")
    assert [s["text"] for s in lines(folder)] == ["Tone1100.", "Tone1600.", "Tone2000."]
    meta = folder.read_meta()
    assert meta["playbacks"] == 1 and meta["playback_segments_dropped"] == 0


def test_note_playback_only_works_while_recording(tmp_path):
    rec, folder, cap, _ = start(tmp_path)
    assert rec.note_playback(2.0, "some words") is True
    rec.stop()
    assert rec.note_playback(2.0, "some words") is False
    assert folder.read_meta()["playbacks"] == 1


# -- before_reopen -------------------------------------------------------------------------------------------


def test_the_recorder_hands_before_reopen_to_the_capture_and_calls_the_controllers_stop(tmp_path):
    calls = []
    holder = {}

    def factory(**kw):
        holder["kw"] = kw
        return ManualCapture(kw["on_audio"])

    comps = Components(transcriber=ToneTranscriber(), scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "m")), comps, before_reopen=lambda: calls.append("stop"))
    rec.start("x")
    holder["kw"]["before_reopen"]()
    assert calls == ["stop"]
    rec.stop()


def test_wasapi_capture_calls_before_reopen_at_the_top_of_a_rebind():
    order = []
    cap = capture_mod.WasapiCapture(lambda s, x: None, before_reopen=lambda: order.append("before_reopen"))
    cap._close = lambda: order.append("close")
    cap._open = lambda: order.append("open")
    cap._rebind("default audio device changed")
    assert order == ["before_reopen", "close", "open"]


def test_a_failing_before_reopen_does_not_stop_the_rebind():
    order = []

    def boom():
        raise RuntimeError("player stuck")

    cap = capture_mod.WasapiCapture(lambda s, x: None, before_reopen=boom)
    cap._close = lambda: order.append("close")
    cap._open = lambda: order.append("open")
    cap._rebind("stream stopped")
    assert order == ["close", "open"]
    # no callback at all is fine too
    plain = capture_mod.WasapiCapture(lambda s, x: None)
    plain._close, plain._open = (lambda: None), (lambda: None)
    plain._rebind("x")


def test_a_line_played_back_just_after_it_was_said_is_kept_because_the_words_span_is_compared(tmp_path):
    """The VAD span of an utterance runs on over the pause after the speech. The user clicking play on a line a
    second after it was said must not make the line itself look like the clip coming back."""
    from huddle_helper.segments import Word
    from huddle_helper.transcribe import Transcription

    class WithWords(GatedTranscriber):
        def transcribe(self, audio, prompt=None):
            r = super().transcribe(audio, prompt)
            return Transcription(r.text, r.confidence, [Word(0, 1900, "Tone1100.")]) if r.text else r

    tr = WithWords()
    rec, folder, cap, _ = start(tmp_path, tr)
    cap.push(silence(1.0), silence(1.0))
    cap.push(silence(2.0), tone(1100, 2.0))  # speech 3.0-5.0 s on the system stream
    cap.push(silence(4.0), silence(4.0))
    rec.playbacks.add(5_300, 2.0, "Tone1100.")  # played back 0.3 s after the speech ended; the window opens at 5.05 s
    tr.go.set()
    rec.stop()
    segs = lines(folder)
    assert [s["text"] for s in segs] == ["Tone1100."] and rec.playbacks.snapshot() == (1, 0)
