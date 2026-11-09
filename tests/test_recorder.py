"""End-to-end recorder runs with fake ASR/embeddings and file-fed audio."""

import os

import numpy as np

from huddle_helper import config, meeting as mt
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone


def run(tmp_path, mic, system, errors=None, **settings):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), **settings)
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": mic, "system": system})
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(),
                       scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(s, comps, on_error=(errors.append if errors is not None else None))
    folder = rec.start("Unit Test")
    holder["cap"].finished.wait()
    rec.stop()
    return folder, read_events(folder.live_path)


def test_two_streams_to_jsonl_contract(tmp_path):
    mic = np.concatenate([silence(0.5), tone(300, 1.5), silence(4), tone(300, 1.0), silence(1)])
    # Remote lines are 2 s: shorter ones can't found a new live speaker (live_min_new_speaker_s).
    system = np.concatenate([silence(2.5), tone(1100, 2.0), silence(0.8), tone(1600, 2.0), silence(2)])
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


def test_meta_has_live_stats_and_quiet_mic_warning(tmp_path):
    # Test #2's mic peaked at 0.047: quiet enough to warn about.
    mic = np.concatenate([silence(0.5), tone(300, 1.5, amp=0.04), silence(1), tone(300, 1.5, amp=0.04), silence(1)])
    errors = []
    folder, _events = run(tmp_path, mic, None, errors=errors)
    stats = folder.read_meta()["live_stats"]
    # Fed faster than real time, so no delay figures (they would be negative).
    assert stats["line_delay_ms"]["lines"] == 0 and stats["line_delay_ms"]["p90"] is None
    assert stats["speech_peak_median"]["mic"] == 0.04 and stats["speech_peak_median"]["system"] is None
    assert len(errors) == 1 and "microphone is very quiet" in errors[0]

    errors = []
    loud = np.concatenate([silence(0.5), tone(300, 1.5), silence(1), tone(300, 1.5), silence(1)])
    run(tmp_path / "loud", loud, None, errors=errors)
    assert errors == []


def test_line_delay_recorded_at_real_time(tmp_path):
    s = config.Settings(meetings_dir=str(tmp_path / "m"))
    audio = np.concatenate([silence(0.3), tone(300, 0.8), silence(1.0)])
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": audio, "system": None}, realtime=True)
        return holder["cap"]

    rec = Recorder(s, Components(transcriber=ToneTranscriber(), scorer_factory=EnergyScorer,
                                 capture_factory=factory))
    folder = rec.start("rt")
    holder["cap"].finished.wait()
    rec.stop()
    delay = folder.read_meta()["live_stats"]["line_delay_ms"]
    assert delay["lines"] == 1 and 0 <= delay["median"] < 3000


def test_echo_dedupe_drops_mic_copy(tmp_path):
    system = np.concatenate([silence(1), tone(700, 2), silence(2)])
    mic = system * 0.4  # speakers leaking into the mic
    _folder, events = run(tmp_path, mic, system)
    segs = [e for e in events if e["type"] == "segment"]
    assert [s["source"] for s in segs] == ["system"]

    _folder, events = run(tmp_path / "b", mic, system, echo_dedupe=False)
    assert sorted(e["source"] for e in events if e["type"] == "segment") == ["mic", "system"]


def test_diarize_mic_and_no_system_diarization(tmp_path):
    mic = np.concatenate([silence(0.5), tone(300, 2.0), silence(1), tone(1300, 2.0), silence(1)])
    system = np.concatenate([silence(6.5), tone(900, 1.0), silence(1)])
    _folder, events = run(tmp_path, mic, system, diarize_mic=True, diarize_system=False)
    speakers = [e["speaker"] for e in events if e["type"] == "segment"]
    assert speakers == ["MIC_SPEAKER_01", "MIC_SPEAKER_02", "Remote"]


def test_silent_meeting(tmp_path):
    folder, events = run(tmp_path, silence(3), None)
    assert [e["type"] for e in events] == ["meeting_start", "meeting_end"]
    assert os.path.getsize(folder.audio_file("system")) > 0
