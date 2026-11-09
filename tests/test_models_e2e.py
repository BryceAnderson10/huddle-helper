"""Full pipeline with the real models on a synthetic TTS meeting.

Slow; runs only when the models are installed and Windows TTS is available:
    pytest -m models
"""

import subprocess
import sys
from pathlib import Path

import pytest

from huddle_helper import models

pytestmark = pytest.mark.models

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    import os

    real_models = Path(os.environ.get("LOCALAPPDATA", "")) / "HuddleHelper" / "models"
    if not (models.whisper_installed("base.en", real_models) and models.diarization_installed(real_models)):
        pytest.skip("models not installed (run: huddle-helper download-models)")
    out = tmp_path_factory.mktemp("sample")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_sample_meeting.py"), "--out", str(out), "--echo"],
                   check=True, capture_output=True)
    return out, real_models


def test_simulated_meeting_end_to_end(sample, tmp_path, monkeypatch):
    out, real_models = sample
    monkeypatch.setenv("HUDDLE_HELPER_DATA_DIR", str(real_models.parent))
    from huddle_helper.cli import main
    from huddle_helper import meeting as mt
    from huddle_helper.writer import read_events

    assert main(["simulate", "--mic", str(out / "mic.wav"), "--system", str(out / "system.wav"),
                 "--out", str(tmp_path), "--title", "e2e"]) == 0
    folder = next(mt.iter_meetings(tmp_path))
    assert folder.is_done and not folder.audio_dir.exists()
    final = [e for e in read_events(folder.final_path) if e["type"] == "segment"]
    mic = [e for e in final if e["source"] == "mic"]
    system = [e for e in final if e["source"] == "system"]
    assert all(e["speaker"] == "You" for e in mic)
    assert len(mic) >= 4  # the echoed remote lines were dropped, not attributed to "You"
    assert not any("legal" in e["text"].lower() for e in mic)
    assert {e["speaker"] for e in system} == {"SPEAKER_01", "SPEAKER_02"}
    text = " ".join(e["text"] for e in final).lower()
    for phrase in ("launch checklist", "terms of service", "maintenance window", "saturday morning"):
        assert phrase in text
