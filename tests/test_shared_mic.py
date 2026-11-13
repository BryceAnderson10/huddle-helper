"""Shared mic (#52): chosen per meeting at Start, stored as stretches in meta.json. Fakes only."""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from huddle_helper import app, cli, config, meeting as mt
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.finalize import append_words, finalize_meeting
from huddle_helper.gui import logic
from huddle_helper.recorder import Components, Recorder
from huddle_helper.segments import Segment, Word
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event, read_events

from .conftest import FakeDiarizer, ToneEmbedder, ToneTranscriber, silence, tone
from .test_gui_logic import model_for, muted_state

MIC = np.concatenate([silence(0.5), tone(300, 2.0), silence(1), tone(1300, 2.0), silence(1)])
SYSTEM = np.concatenate([silence(6.5), tone(900, 1.0), silence(1)])


def record(tmp_path, shared_mic, during=None, **settings):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), keep_audio=True, **settings)
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": MIC, "system": SYSTEM})
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(),
                       scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(s, comps)
    folder = rec.start("Unit Test", shared_mic=shared_mic)
    holder["cap"].finished.wait()
    if during is not None:
        during(folder)
    rec.stop()
    return folder


def speakers(path, source="mic"):
    return [e["speaker"] for e in read_events(path) if e["type"] == "segment" and e["source"] == source]


# -- Settings --------------------------------------------------------------------------------------
def test_settings_drop_the_old_check_box_value():
    assert not hasattr(config.Settings(), "diarize_mic")
    s = config.Settings.from_dict({"diarize_mic": True, "diarize_system": False})
    assert not hasattr(s, "diarize_mic") and "diarize_mic" not in s.to_dict()
    assert s.needs_diarization is False


def test_saved_settings_file_with_diarize_mic_loads(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**config.Settings().to_dict(), "diarize_mic": True}), encoding="utf-8")
    s = config.load(path)
    assert "diarize_mic" not in s.to_dict()


# -- Recorder + final pass -----------------------------------------------------------------------------
def test_ticked_at_start_separates_the_mic_live_and_in_the_final_transcript(tmp_path):
    seen = {}
    folder = record(tmp_path, True, during=lambda f: seen.update(f.read_meta()),
                    diarize_system=False)
    assert seen["shared_mic"] == [[0, None]]  # open while recording
    assert seen["settings"]["diarize_mic"] is True
    meta = folder.read_meta()
    (start, end), = meta["shared_mic"]
    assert start == 0 and isinstance(end, int) and end >= 6000  # closed at the meeting's end on Stop
    assert meta["settings"]["diarize_mic"] is True
    assert speakers(folder.live_path) == ["MIC_SPEAKER_01", "MIC_SPEAKER_02"]

    finalize_meeting(folder, FakeDiarizer, default_keep_audio=False)
    final = speakers(folder.final_path)
    assert final and all(sp.startswith("MIC_SPEAKER_") for sp in final)
    assert "mic" in folder.read_meta()["refined_streams"]


def test_not_ticked_keeps_every_mic_line_as_you(tmp_path):
    seen = {}
    folder = record(tmp_path, False, during=lambda f: seen.update(f.read_meta()))
    assert seen["shared_mic"] == []
    meta = folder.read_meta()
    assert meta["shared_mic"] == [] and meta["settings"]["diarize_mic"] is False
    assert speakers(folder.live_path) == ["You", "You"]
    finalize_meeting(folder, FakeDiarizer, default_keep_audio=False)
    assert speakers(folder.final_path) == ["You", "You"]
    assert "mic" not in folder.read_meta()["refined_streams"]


def test_old_meeting_with_the_flag_and_no_list_is_separated_whole(tmp_path):
    import soundfile as sf

    started = mt.now_local()
    folder = mt.MeetingFolder.create(tmp_path, "Old", started, {
        "settings": {"diarize_system": False, "diarize_mic": True, "keep_audio": False}})
    assert "shared_mic" not in folder.read_meta()
    sf.write(str(folder.audio_file("mic")), np.zeros(16000 * 3, np.float32), 16000, format="FLAC")
    segs = [Segment("mic", 500, 1500, "First.", 0.9, "MIC_SPEAKER_01", [Word(500, 1500, " First.")]),
            Segment("mic", 2500, 3000, "Second.", 0.9, "MIC_SPEAKER_01", [Word(2500, 3000, " Second.")])]
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, mt.iso(started)))
        for i, s in enumerate(segs, 1):
            w.write(s.to_event(i))
            append_words(folder.words_path, s)
        w.write(meeting_end_event(mt.iso(started), len(segs)))
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)

    finalize_meeting(folder, FakeDiarizer, default_keep_audio=False)
    assert speakers(folder.final_path) == ["MIC_SPEAKER_01", "MIC_SPEAKER_02"]
    assert folder.read_meta()["refined_streams"] == ["mic"]


# -- CLI -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("flag", [True, False])
def test_cli_diarize_mic_is_shared_mic_from_the_start(tmp_path, monkeypatch, flag):
    import soundfile as sf

    from huddle_helper import recorder

    sf.write(str(tmp_path / "mic.wav"), MIC, 16000)
    sf.write(str(tmp_path / "system.wav"), SYSTEM, 16000)
    loaded = {}

    def fake_load(s, models_root=None, shared_mic=False):
        loaded["shared_mic"] = shared_mic
        return Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer)

    finalized = []
    monkeypatch.setattr(recorder, "load_components", fake_load)
    monkeypatch.setattr(cli, "_ensure_models", lambda s, shared=False: None)
    monkeypatch.setattr(cli, "go_offline", lambda: None)
    monkeypatch.setattr(cli, "_finalize", lambda folder, s: finalized.append(folder))
    argv = ["simulate", "--mic", str(tmp_path / "mic.wav"), "--system", str(tmp_path / "system.wav"),
            "--out", str(tmp_path / "meetings")] + (["--diarize-mic"] if flag else [])
    assert cli.main(argv) == 0
    folder, = finalized
    meta = folder.read_meta()
    assert loaded["shared_mic"] is flag
    assert meta["settings"]["diarize_mic"] is flag
    assert (meta["shared_mic"][0][0] == 0 and meta["shared_mic"][0][1] is not None) if flag \
        else meta["shared_mic"] == []
    assert all(sp.startswith("MIC_SPEAKER_") if flag else sp == "You" for sp in speakers(folder.live_path))


# -- Controller ----------------------------------------------------------------------------------------
def test_controller_passes_shared_mic_and_loads_the_voice_model(tmp_path, monkeypatch):
    seen = {}

    class FakeRecorder:
        def __init__(self, *a, **kw):
            pass

        def start(self, title, terms=(), start_muted=False, attendees=(), shared_mic=False):
            seen["shared_mic"] = shared_mic
            return SimpleNamespace(meeting_id="m1")

    monkeypatch.setattr(app, "Recorder", FakeRecorder)
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path), "diarize_system": False})
    loads = []
    monkeypatch.setattr(app, "load_components",
                        lambda s, shared_mic=False: loads.append(shared_mic)
                        or Components(transcriber=None, embedder=ToneEmbedder() if shared_mic else None))
    ctl.state.phase = "ready"
    ctl.start("t", [], [], False, True)  # the Start dialog's positional call
    for _ in range(200):
        if ctl.state.phase == "recording":
            break
        time.sleep(0.02)
    assert seen["shared_mic"] is True and loads == [True]
    ctl._get_components()  # the voice model already loaded is reused for a meeting without a shared mic
    ctl._get_components(shared_mic=True)
    assert loads == [True]


# -- Mute tooltip ------------------------------------------------------------------------------------------
def test_mute_tooltip_warns_for_a_meeting_started_with_the_box_ticked(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    st = muted_state(mid)
    st.recording.shared_mic = True  # the recorder's live status
    assert "everyone in the room is muted" in logic.mute_button_view(mid, m, st).tooltip
    st.recording.shared_mic = False
    assert "everyone in the room" not in logic.mute_button_view(mid, m, st).tooltip
