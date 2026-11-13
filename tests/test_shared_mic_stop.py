"""Where Stop ended a shared mic stretch (#60): the meeting's end on the meeting clock is ``ended_ms``, the
same value Stop closed open stretches at, so the live view never mistakes it for an untick. Fakes only."""

import time

import numpy as np

from huddle_helper import config
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.recorder import Components, Recorder
from huddle_helper.transcript_model import TranscriptModel
from huddle_helper.vad import EnergyScorer

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone

MIC = np.concatenate([silence(0.3), tone(300, 1.0), silence(0.5)])


class SlowTranscriber(ToneTranscriber):
    """Takes ``delay_s`` per line, like the target laptop at Stop: the backlog drains after capture ends."""

    def __init__(self, delay_s):
        super().__init__()
        self.delay_s = delay_s

    def transcribe(self, audio, prompt=None):
        time.sleep(self.delay_s)
        return super().transcribe(audio, prompt)


def test_stop_with_a_slow_backlog_draws_no_just_you_divider_at_the_end(tmp_path):
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), keep_audio=True, diarize_system=False)
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": MIC, "system": None}, realtime=True)
        return holder["cap"]

    rec = Recorder(s, Components(transcriber=SlowTranscriber(1.5), embedder=ToneEmbedder(),
                                 scorer_factory=EnergyScorer, capture_factory=factory))
    folder = rec.start("Unit Test", shared_mic=True)
    holder["cap"].finished.wait()
    rec.stop()

    meta = folder.read_meta()
    assert meta["duration_s"] * 1000 > meta["shared_mic"][0][1] + 1000  # the backlog outlasted the old margin
    assert meta["ended_ms"] == meta["shared_mic"][0][1]
    m = TranscriptModel.load(folder)
    assert [(d.at_ms, d.on) for d in m.dividers] == [(0, True)]
