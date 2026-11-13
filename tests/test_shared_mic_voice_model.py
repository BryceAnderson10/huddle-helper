"""Shared mic when the voice model is not in memory (#55): loaded in the background when the box is ticked
mid-meeting, before recording when it is ticked at Start, a failed load, and the box disabled when the model is
not downloaded. Fakes only: the loader hands out a ToneEmbedder or raises."""

import threading
import time
from types import SimpleNamespace

import pytest

from huddle_helper import app, config, meeting as mt, models
from huddle_helper.app import AppState
from huddle_helper.gui import logic
from huddle_helper.recorder import Components, Recorder, Status
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber
from .helpers import make_meeting
from .test_gui_logic import model_for, recording_state
from .test_shared_mic_final import RecordingDiarizer
from .test_shared_mic_toggle import MIC, HeldCapture, mic_lines, root  # noqa: F401 - root is a fixture

INFO = {"diarization_segmentation": models.SEGMENTATION_NAME, "diarization_embedding": models.EMBEDDING_NAME}


class GatedLoader:
    """A voice model loader that blocks until ``release()``, then returns a ToneEmbedder (or raises ``error``)."""

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error
        self.gate = threading.Event()
        self.done = threading.Event()

    def __call__(self):
        self.calls += 1
        try:
            self.gate.wait(10)
            if self.error is not None:
                raise self.error
            return ToneEmbedder()
        finally:
            self.done.set()

    def release(self):
        self.gate.set()


class ReleasingTranscriber(ToneTranscriber):
    """Releases the loader when it is asked for line number ``at`` and waits until the load has finished, so the
    lines before it are labelled while the model is still loading and the ones from it on after."""

    def __init__(self, loader: GatedLoader, comps_ref: dict, at: int):
        super().__init__()
        self.loader, self.comps_ref, self.at, self.n = loader, comps_ref, at, 0

    def transcribe(self, audio, prompt=None):
        self.n += 1
        if self.n == self.at:
            self.loader.release()
            assert self.loader.done.wait(10)
            for _ in range(500):  # the recorder's load thread finishes just after the loader returns
                if self.comps_ref["rec"].status.voice_model != "loading":
                    break
                time.sleep(0.01)
        return super().transcribe(audio, prompt)


def record(tmp_path, loader, toggles=(), *, shared_mic=False, release_at=None, embedder=None,
           diarize_system=False, during=None):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), keep_audio=True, diarize_system=diarize_system)
    holder, ref = {}, {}

    def factory(**kw):
        holder["cap"] = HeldCapture(kw["on_audio"], {"mic": MIC, "system": None})
        return holder["cap"]

    transcriber = ReleasingTranscriber(loader, ref, release_at) if release_at else ToneTranscriber()
    comps = ref["c"] = Components(transcriber=transcriber, embedder=embedder, scorer_factory=EnergyScorer,
                                  capture_factory=factory, embedder_loader=loader, embedder_info=dict(INFO))
    statuses = []
    rec = ref["rec"] = Recorder(s, comps,
                                on_status=lambda st: statuses.append((st.voice_model, st.voice_model_error)))
    folder = rec.start("Unit Test", shared_mic=shared_mic)
    for on, at in toggles:
        rec.set_shared_mic(on, at_ms=at)
    holder["cap"].release()
    holder["cap"].finished.wait()
    if during is not None:
        during(folder, rec)
    rec.stop()
    return folder, rec, statuses


# -- background load ---------------------------------------------------------------------------------
def test_ticked_mid_meeting_loads_in_the_background_without_stalling_live_lines(tmp_path):
    loader = GatedLoader()
    # Ticked at 3 s; the model becomes ready while the third mic line (6.5 s) is being transcribed.
    folder, rec, statuses = record(tmp_path, loader, [(True, 3000)], release_at=3)
    assert loader.calls == 1
    # Lines 1 (before the stretch) and 2 (in it, model still loading) were written while the load was blocked:
    # live transcription did not wait for it. From line 3 on, voices A and B are grouped.
    assert [sp for _, sp in mic_lines(folder)] == ["You", "You", "MIC_SPEAKER_01", "MIC_SPEAKER_02",
                                                   "MIC_SPEAKER_01"]
    meta = folder.read_meta()
    assert meta["shared_mic"][0][0] == 3000  # the stretch starts at the click, not when the model was ready
    assert {k: meta["models"][k] for k in INFO} == INFO  # loaded part-way through: named in meta.json
    assert ("loading", "") in statuses and ("ready", "") in statuses
    assert rec.c.embedder is not None  # kept in the components for the next meeting


def test_final_pass_separates_the_whole_stretch_including_lines_shown_as_you(tmp_path):
    from huddle_helper.finalize import finalize_meeting

    loader = GatedLoader()
    folder, _rec, _ = record(tmp_path, loader, [(True, 3000)], release_at=3)
    finalize_meeting(folder, RecordingDiarizer, keep_audio=False, embedder_factory=ToneEmbedder)
    final = [(e["start_ms"], e["speaker"]) for e in read_events(folder.final_path)
             if e["type"] == "segment" and e["source"] == "mic"]
    assert final[0][1] == "You" and final[0][0] < 3000
    inside = [sp for start, sp in final if start >= 3000]
    assert len(inside) == 4 and all(sp.startswith("MIC_SPEAKER_") for sp in inside)
    assert inside[0] == inside[2] != inside[1]  # the line shown live as You is grouped with its voice (B)


def test_ticking_again_while_loading_does_not_start_a_second_load(tmp_path):
    loader = GatedLoader()

    def toggle_more(folder, rec):
        rec.set_shared_mic(False, at_ms=14_000)
        rec.set_shared_mic(True, at_ms=14_100)

    folder, rec, _ = record(tmp_path, loader, [(True, 3000)], during=toggle_more)
    assert loader.calls == 1
    assert all(sp == "You" for _, sp in mic_lines(folder))  # never released: every line stays You
    loader.release()


# -- failed load ------------------------------------------------------------------------------------------
def test_a_failed_load_is_reported_keeps_the_stretch_and_live_lines_go_on(tmp_path):
    loader = GatedLoader(error=RuntimeError("model file damaged"))
    seen = {}

    def look(folder, rec):
        for _ in range(500):  # the third line, which releases the load, may still be waiting to be transcribed
            if rec.status.voice_model != "loading":
                break
            time.sleep(0.01)
        seen["status"] = (rec.status.voice_model, rec.status.voice_model_error)
        seen["notice"] = logic.notice_view(folder.meeting_id, model_for_folder(folder),
                                           SimpleNamespace(phase="recording", recording=rec.status,
                                                           last_meeting=folder.meeting_id, finalize=None,
                                                           hotkey_problems=[]))

    folder, rec, _ = record(tmp_path, loader, [(True, 3000)], release_at=3, during=look)
    assert seen["status"] == ("failed", "model file damaged")
    assert seen["notice"].level == "warn" and "Could not load the voice model (model file damaged)" in seen["notice"].text
    assert [sp for _, sp in mic_lines(folder)] == ["You"] * 5  # all five lines written, the mic ones as You
    meta = folder.read_meta()
    assert meta["shared_mic"][0][0] == 3000 and meta["settings"]["diarize_mic"] is True  # the stretch is kept
    assert "diarization_embedding" not in meta["models"]


def model_for_folder(folder):
    from huddle_helper.transcript_model import TranscriptModel

    return TranscriptModel.load(folder)


def test_loading_notice_shows_only_for_the_meeting_being_recorded(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    st = recording_state(mid)
    st.recording = Status(state="recording", meeting_id=mid, voice_model="loading")
    n = logic.notice_view(mid, m, st)
    assert n.level == "info" and "Loading the voice model" in n.text and "Provisional" in n.text
    st.recording = Status(state="recording", meeting_id="another", voice_model="failed")
    assert "voice model" not in logic.notice_view(mid, m, st).text
    st.recording = Status(state="recording", meeting_id=mid, voice_model="ready")
    assert "voice model" not in logic.notice_view(mid, m, st).text


# -- ticked at Start / remote separation on -----------------------------------------------------------
def test_ticked_at_start_loads_the_model_before_recording_and_the_first_line_is_grouped(tmp_path):
    loader = GatedLoader()
    loader.release()
    folder, rec, statuses = record(tmp_path, loader, shared_mic=True)
    assert loader.calls == 1
    lines = mic_lines(folder)
    assert lines[0][1] == "MIC_SPEAKER_01" and all(sp.startswith("MIC_SPEAKER_") for _, sp in lines)
    assert {k: folder.read_meta()["models"][k] for k in INFO} == INFO
    assert all(v == "" for v, _ in statuses)  # no background load, no notice


def test_with_the_model_already_loaded_the_loader_is_not_used(tmp_path):
    loader = GatedLoader(error=AssertionError("must not load"))
    folder, rec, statuses = record(tmp_path, loader, [(True, 3000)], embedder=ToneEmbedder(), diarize_system=True)
    assert loader.calls == 0 and all(v == "" for v, _ in statuses)
    assert [sp for _, sp in mic_lines(folder)] == ["You", "MIC_SPEAKER_01", "MIC_SPEAKER_02", "MIC_SPEAKER_01",
                                                   "MIC_SPEAKER_02"]


def test_shared_mic_without_any_voice_model_labels_mic_lines_you(tmp_path):
    folder, _rec, _ = record(tmp_path, None, shared_mic=True)
    assert [sp for _, sp in mic_lines(folder)] == ["You"] * 5


# -- not downloaded ----------------------------------------------------------------------------------------
def test_not_downloaded_disables_the_window_box_with_a_tooltip(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    st = recording_state(mid)
    st.voice_model_installed = False
    v = logic.shared_mic_view(mid, m, st)
    assert v.visible and not v.enabled and not v.checked
    assert "voice model is not downloaded" in v.tooltip and "download-models" in v.tooltip
    st.voice_model_installed = True
    assert logic.shared_mic_view(mid, m, st).enabled


def test_the_loader_never_downloads_a_missing_model(monkeypatch):
    from huddle_helper import recorder, transcribe

    monkeypatch.setattr(models, "diarization_installed", lambda root=None: False)
    for name in ("install_all", "install_diarization", "install_whisper"):
        monkeypatch.setattr(models, name, lambda *a, **kw: pytest.fail("downloaded during a meeting"))
    monkeypatch.setattr(transcribe, "WhisperTranscriber", lambda *a, **kw: SimpleNamespace(name="fake"))
    comps = recorder.load_components(config.Settings(diarize_system=False))
    assert comps.embedder is None and comps.embedder_info == INFO
    with pytest.raises(FileNotFoundError, match="not downloaded"):
        comps.embedder_loader()


def test_controller_tracks_whether_the_voice_model_is_installed(tmp_path, monkeypatch):
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)})
    monkeypatch.setattr(models, "diarization_installed", lambda root=None: False)
    assert ctl.voice_model_installed() is False and ctl.state.voice_model_installed is False
    monkeypatch.setattr(models, "diarization_installed", lambda root=None: True)
    assert ctl.voice_model_installed() is True and ctl.state.voice_model_installed is True


def test_controller_reuses_components_whose_model_loads_at_start(tmp_path, monkeypatch):
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path), "diarize_system": False})
    loads = []
    monkeypatch.setattr(app, "load_components", lambda s, shared_mic=False: loads.append(shared_mic) or Components(
        transcriber=None, embedder_loader=ToneEmbedder))
    first = ctl._get_components()
    assert ctl._get_components(shared_mic=True) is first and loads == [False]  # Whisper is not loaded again


def test_window_box_is_disabled_when_the_model_is_not_downloaded(root, tmp_path):
    from huddle_helper.gui.window import MainWindow

    from .test_shared_mic_toggle import FakeApp

    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "hello.")], status=mt.STATUS_RECORDING)
    folder.update_meta(shared_mic=[])
    state = AppState(phase="recording", last_meeting=folder.meeting_id, voice_model_installed=False)
    state.recording = Status(state="recording", meeting_id=folder.meeting_id, elapsed_s=10, segments=1)
    w = MainWindow(root, FakeApp(tmp_path, state, folder))
    w.show(folder)
    root.update()
    assert w.shared_chk.winfo_ismapped() and w.shared_chk.instate(["disabled"])
    assert "not downloaded" in w.shared_tip.text
    state.voice_model_installed = True
    w._refresh_all()
    assert not w.shared_chk.instate(["disabled"])
    w.destroy()


def test_start_dialog_box_is_disabled_when_the_model_is_not_downloaded(root):
    from huddle_helper.ui import StartDialog

    d = StartDialog(root, lambda *a: None, voice_model_installed=False)
    assert d.shared_chk.instate(["disabled"]) and "not downloaded" in d.shared_tip.text
    d.win.destroy()
    d = StartDialog(root, lambda *a: None)
    assert not d.shared_chk.instate(["disabled"])
    d.win.destroy()
