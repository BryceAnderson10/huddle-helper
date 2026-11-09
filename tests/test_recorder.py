"""End-to-end recorder runs with fake ASR/embeddings and file-fed audio."""

import os

import numpy as np

from huddle_helper import config, meeting as mt
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone


def run(tmp_path, mic, system, **settings):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), **settings)
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": mic, "system": system})
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(),
                       scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(s, comps)
    folder = rec.start("Unit Test")
    holder["cap"].finished.wait()
    rec.stop()
    return folder, read_events(folder.live_path)


def test_two_streams_to_jsonl_contract(tmp_path):
    mic = np.concatenate([silence(0.5), tone(300, 1.5), silence(4), tone(300, 1.0), silence(1)])
    system = np.concatenate([silence(2.5), tone(1100, 1.5), silence(0.8), tone(1600, 1.5), silence(2)])
    folder, events = run(tmp_path, mic, system)

    assert events[0]["type"] == "meeting_start" and events[0]["meeting_id"] == folder.meeting_id
    assert events[0]["schema"] == 1
    assert events[-1]["type"] == "meeting_end" and events[-1]["segments"] == len(events) - 2
    segs = events[1:-1]
    assert [s["seq"] for s in segs] == list(range(1, len(segs) + 1))
    assert [s["start_ms"] for s in segs] == sorted(s["start_ms"] for s in segs)
    assert [(s["source"], s["speaker"]) for s in segs] == [
        ("mic", "You"), ("system", "SPEAKER_01"), ("system", "SPEAKER_02"), ("mic", "You")]
    assert segs[1]["text"] == "Tone1100."
    assert 2250 <= segs[1]["start_ms"] <= 2600  # shared clock across streams (32 ms VAD frames)

    meta = folder.read_meta()
    assert meta["status"] == "processing" and meta["clean_exit"] is True and meta["segments"] == 4
    assert folder.audio_file("mic").exists() and folder.audio_file("system").exists()
    assert not folder.done_path.exists()  # DONE only after the final pass


def test_echo_dedupe_drops_mic_copy(tmp_path):
    system = np.concatenate([silence(1), tone(700, 2), silence(2)])
    mic = system * 0.4  # speakers leaking into the mic
    _folder, events = run(tmp_path, mic, system)
    segs = [e for e in events if e["type"] == "segment"]
    assert [s["source"] for s in segs] == ["system"]

    _folder, events = run(tmp_path / "b", mic, system, echo_dedupe=False)
    assert sorted(e["source"] for e in events if e["type"] == "segment") == ["mic", "system"]


def test_diarize_mic_and_no_system_diarization(tmp_path):
    mic = np.concatenate([silence(0.5), tone(300, 1.5), silence(1), tone(1300, 1.5), silence(1)])
    system = np.concatenate([silence(5.5), tone(900, 1.0), silence(1)])
    _folder, events = run(tmp_path, mic, system, diarize_mic=True, diarize_system=False)
    speakers = [e["speaker"] for e in events if e["type"] == "segment"]
    assert speakers == ["MIC_SPEAKER_01", "MIC_SPEAKER_02", "Remote"]


def test_silent_meeting(tmp_path):
    folder, events = run(tmp_path, silence(3), None)
    assert [e["type"] for e in events] == ["meeting_start", "meeting_end"]
    assert os.path.getsize(folder.audio_file("system")) > 0
