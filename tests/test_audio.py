import numpy as np

from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.audio.aligner import StreamAligner
from huddle_helper.audio.buffer import FlacBuffer, read_audio
from huddle_helper.vad import EnergyScorer, UtteranceSegmenter

from .conftest import silence, tone


def test_aligner_pads_gaps_and_idle_time():
    got = []
    a = StreamAligner(t0=100.0, on_samples=got.append)
    a.push(100.1, np.ones(1600, np.float32))  # 0.1 s arriving on time
    assert a.pos == 1600 and a.padded == 0
    a.push(101.1, np.ones(1600, np.float32))  # 0.9 s silent gap (loopback with nothing playing)
    assert a.pos == SAMPLE_RATE + 1600
    assert a.padded == int(0.9 * SAMPLE_RATE)
    a.tick(103.0)  # nothing for a while: pad up to now - idle latency
    assert a.pos == int(2.5 * SAMPLE_RATE)
    a.finish(103.0)
    assert a.pos == 3 * SAMPLE_RATE


def test_flac_buffer_roundtrip(tmp_path):
    buf = FlacBuffer(tmp_path / "a" / "mic.flac")
    x = tone(440, 1.0)
    buf.write(x[:8000])
    buf.write(x[8000:])
    buf.close()
    y = read_audio(tmp_path / "a" / "mic.flac")
    assert len(y) == len(x)
    assert np.abs(y - x).max() < 1e-3


def _segment(audio, **kw):
    utts = []
    seg = UtteranceSegmenter("mic", utts.append, scorer=EnergyScorer(), **kw)
    for i in range(0, len(audio), 1000):  # feed in small, odd-sized chunks
        seg.feed(audio[i:i + 1000])
    seg.flush()
    return utts, seg


def test_segmenter_cuts_at_pauses():
    audio = np.concatenate([silence(1), tone(300, 2), silence(1.5), tone(600, 1.5), silence(1)])
    utts, _ = _segment(audio)
    assert len(utts) == 2
    assert 700 <= utts[0].start_ms <= 1000 and 3000 <= utts[0].end_ms <= 3400
    assert 4250 <= utts[1].start_ms <= 4500
    for u in utts:
        assert len(u.audio) == u.end - u.start


def test_segmenter_splits_long_speech():
    utts, _ = _segment(np.concatenate([tone(300, 20), silence(1)]), max_utterance_s=8)
    assert len(utts) == 3
    assert all(u.duration_s <= 8.1 for u in utts)
    assert utts[1].start == utts[0].end  # no audio lost between pieces


def test_segmenter_flush_closes_open_utterance_and_ignores_blips():
    audio = np.concatenate([silence(0.5), tone(300, 0.1), silence(1), tone(300, 1)])
    utts, _ = _segment(audio)
    assert len(utts) == 1  # the 100 ms blip is below min speech
    assert utts[0].end == len(audio)


def test_watermark_never_passes_unemitted_speech():
    utts = []
    seg = UtteranceSegmenter("mic", utts.append, scorer=EnergyScorer())
    seg.feed(silence(1))
    assert seg.watermark > 0
    seg.feed(tone(300, 1))
    assert not utts
    assert seg.watermark <= SAMPLE_RATE  # held at the speech start
    seg.feed(silence(1))
    assert len(utts) == 1 and seg.watermark >= utts[0].end
