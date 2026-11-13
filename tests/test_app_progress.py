import numpy as np
import pytest

from huddle_helper import config
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.recorder import Components, Recorder, Status
from huddle_helper.vad import EnergyScorer

from .conftest import ToneEmbedder, ToneTranscriber


# -- levels -------------------------------------------------------------------
def recorder(tmp_path):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"))
    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer)
    return Recorder(s, comps)


def test_status_has_levels_for_both_streams():
    assert Status().levels == {"mic": 0.0, "system": 0.0}
    assert Status().levels is not Status().levels


def test_levels_follow_the_peak_and_decay(tmp_path, monkeypatch):
    rec = recorder(tmp_path)
    clock = [100.0]
    monkeypatch.setattr("huddle_helper.recorder.time.perf_counter", lambda: clock[0])
    loud = np.full(1600, 0.0, np.float32)
    loud[10] = -0.8
    rec._on_audio("mic", loud)
    assert rec._current_levels() == {"mic": 0.8, "system": 0.0}
    clock[0] += rec.LEVEL_HALF_LIFE_S
    assert rec._current_levels()["mic"] == pytest.approx(0.4, abs=0.001)
    rec._on_audio("mic", np.full(1600, 0.1, np.float32))  # quieter chunk: the held, decayed peak stays
    assert rec._current_levels()["mic"] == pytest.approx(0.4, abs=0.001)
    clock[0] += 10 * rec.LEVEL_HALF_LIFE_S
    assert rec._current_levels()["mic"] < 0.001
    rec._on_audio("system", np.zeros(0, np.float32))  # empty chunk is harmless
    assert rec._current_levels()["system"] == 0.0


def test_published_status_carries_levels(tmp_path):
    rec = recorder(tmp_path)
    seen = []
    rec._on_status = seen.append
    rec.status = Status(state="recording")
    rec._t0 = 1.0
    rec._on_audio("system", np.full(160, 0.5, np.float32))
    rec._publish()
    assert seen[-1].levels["system"] > 0.4 and seen[-1].levels["mic"] == 0.0
