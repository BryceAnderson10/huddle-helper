"""Mute and bookmarks through the real Recorder (fake ASR, hand-fed audio) and crash recovery of an open mute."""

import numpy as np

from huddle_helper import config, housekeeping, meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.audio.buffer import FlacBuffer, read_audio
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone
from .helpers import make_meeting

CHUNK = SAMPLE_RATE // 10  # 100 ms, like the file capture


class ManualCapture:
    """Stands in for the devices: the test is the capture thread and delivers audio when it says so."""

    realtime = False
    device_names = {"mic": "test", "system": "test"}

    def __init__(self, on_audio, **_):
        self.on_audio = on_audio

    def start(self, t0):
        pass

    def stop(self):
        pass

    def push(self, mic: np.ndarray, system: np.ndarray) -> None:
        n = max(len(mic), len(system))
        mic = np.concatenate([mic, np.zeros(n - len(mic), np.float32)])
        system = np.concatenate([system, np.zeros(n - len(system), np.float32)])
        for i in range(0, n, CHUNK):
            self.on_audio("mic", mic[i:i + CHUNK])
            self.on_audio("system", system[i:i + CHUNK])


def start(tmp_path, **kw):
    holder = {}

    def factory(**fkw):
        holder["cap"] = ManualCapture(fkw["on_audio"])
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer,
                       capture_factory=factory)
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "meetings")), comps)
    folder = rec.start("Mute Test", **kw)
    return rec, folder, holder["cap"]


def hiss(seconds: float) -> np.ndarray:
    """Quiet room noise, never an exact zero once stored as 16-bit, and far below the VAD threshold."""
    n = int(seconds * SAMPLE_RATE)
    return (0.002 + np.random.default_rng(7).uniform(-0.001, 0.001, n)).astype(np.float32)


def zero_runs(x: np.ndarray) -> list[tuple[int, int]]:
    """(first, last+1) sample of every run of exact zeros."""
    z = np.concatenate([[False], x == 0, [False]])
    d = np.diff(z.astype(np.int8))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def mic_lines(folder):
    return [e for e in read_events(folder.live_path) if e.get("type") == "segment" and e["source"] == "mic"]


def test_a_meeting_started_muted_has_no_mic_lines_and_a_silent_mic_flac(tmp_path):
    rec, folder, cap = start(tmp_path, start_muted=True)
    assert folder.read_meta()["mic_muted_since_ms"] == 0 and rec.mic_muted
    mic = np.concatenate([silence(0.5), tone(300, 2.0), silence(1), tone(300, 1.0), silence(1)])
    system = np.concatenate([silence(2.5), tone(1100, 2.0), silence(2)])
    cap.push(mic, system)
    rec.stop()

    segs = [e for e in read_events(folder.live_path) if e.get("type") == "segment"]
    assert [(s["source"], s["text"]) for s in segs] == [("system", "Tone1100.")]  # system stream unaffected
    on_disk = read_audio(folder.audio_file("mic"))
    assert len(on_disk) == len(system) and not on_disk.any()  # both streams share one clock
    sys_audio = read_audio(folder.audio_file("system"))
    assert len(sys_audio) == len(system) and np.abs(sys_audio).max() > 0.25
    meta = folder.read_meta()
    assert meta["gaps"] == [{"source": "mic", "start_ms": 0, "end_ms": len(system) * 1000 // SAMPLE_RATE,
                             "reason": "muted"}]
    assert meta["mic_muted_since_ms"] is None and meta["status"] == "processing"
    assert not rec.mic_muted and rec.status.mic_muted is False


def test_mute_and_unmute_mid_meeting_match_the_flac_exactly(tmp_path):
    rec, folder, cap = start(tmp_path)
    speech = tone(300, 1.5)
    cap.push(np.concatenate([hiss(0.5), speech, hiss(3.0)]), silence(5.0))  # 0-5 s, open
    assert rec.set_mic_muted(True) is True
    assert folder.read_meta()["mic_muted_since_ms"] == 5000  # written at once, for a live follower
    cap.push(np.concatenate([hiss(1.0), speech, hiss(1.5)]), silence(4.0))  # 5-9 s, muted
    assert rec.set_mic_muted(False) is False
    cap.push(np.concatenate([hiss(1.0), speech, hiss(1.5)]), silence(4.0))  # 9-13 s, open again
    rec.stop()

    # three speech bursts were played into the mic, the one under the mute is gone
    starts = [e["start_ms"] for e in mic_lines(folder)]
    assert len(starts) == 2 and starts[0] < 1000 and 9500 < starts[1] < 10_200
    on_disk = read_audio(folder.audio_file("mic"))
    assert len(on_disk) == 13 * SAMPLE_RATE
    (gap,) = folder.read_meta()["gaps"]
    assert gap == {"source": "mic", "start_ms": 5000, "end_ms": 9250, "reason": "muted"}  # 4 s + the 250 ms tail
    # the zeros in the file cover the gap exactly: [5.000, 9.250) s
    assert (on_disk[5 * SAMPLE_RATE:9 * SAMPLE_RATE + 4000] == 0).all()
    assert np.abs(on_disk[9 * SAMPLE_RATE + 4000:]).max() > 0.25  # the next burst starts at 10 s
    # the only long run of exact zeros is the gap (a tone's zero crossings are 1-2 samples): it starts and
    # ends on the samples the gap says
    assert [(int(a), int(b)) for a, b in zero_runs(on_disk) if b - a > 100] == [(5 * SAMPLE_RATE, 9 * SAMPLE_RATE + 4000)]
    assert folder.read_meta()["mic_muted_since_ms"] is None
    assert folder.read_meta()["marks"] == 0 and folder.read_meta()["mark_kinds"] == {}


def test_stop_while_muted_closes_the_period_at_the_end_of_the_audio_and_unmutes(tmp_path):
    rec, folder, cap = start(tmp_path)
    cap.push(tone(300, 1.5), silence(3.0))
    rec.set_mic_muted(True)
    cap.push(tone(300, 1.5), silence(2.0))
    assert rec.mic_muted
    rec.stop()
    meta = folder.read_meta()
    assert meta["gaps"] == [{"source": "mic", "start_ms": 3000, "end_ms": 5000, "reason": "muted"}]
    assert meta["mic_muted_since_ms"] is None and not rec.mic_muted and not rec.gate.zeroing
    assert [e["start_ms"] for e in mic_lines(folder)] and len(mic_lines(folder)) == 1


def test_mute_does_nothing_when_not_recording_and_levels_show_silence(tmp_path):
    rec, folder, cap = start(tmp_path)
    rec.set_mic_muted(True)
    cap.push(tone(300, 1.0), silence(1.0))
    assert rec._current_levels()["mic"] == 0.0 and rec.status.mic_muted
    rec.stop()
    assert rec.set_mic_muted(True) is False and rec.now_ms() is None and rec.bookmark() is None


def test_bookmarks_count_in_status_and_meta_at_stop(tmp_path):
    rec, folder, cap = start(tmp_path)
    cap.push(tone(300, 1.0), silence(1.0))
    assert 0 <= rec.now_ms() < 5000
    first = rec.bookmark("decision")
    assert first.action == "added" and first.at_ms <= rec.now_ms()
    assert rec.bookmark("decision").action == "ignored"  # the 2 s rule
    assert rec.bookmark("question").action == "retyped"
    assert rec.bookmark("action", "line", 1234, 7).action == "added"
    rec._publish()
    assert rec.status.marks == 2
    rec.stop()
    meta = folder.read_meta()
    assert meta["marks"] == 2 and meta["mark_kinds"] == {}
    events = read_events(folder.marks_path)
    assert [(e["type"], e.get("kind")) for e in events] == [("mark", "decision"), ("mark_kind", "question"),
                                                              ("mark", "action")]
    assert events[2]["via"] == "line" and events[2]["seq"] == 7 and events[2]["at_ms"] == 1234


# -- crash recovery ------------------------------------------------------------------------------------
def crashed_while_muted(tmp_path, *, audio_s: float | None, last_line_end: int = 40_000):
    folder = make_meeting(tmp_path, [("system", 30_000, last_line_end, "SPEAKER_00", "hello.")],
                          status=mt.STATUS_RECORDING)
    folder.update_meta(clean_exit=None, ended_at=None, mic_muted_since_ms=20_000,
                       gaps=[{"source": "mic", "start_ms": 1000, "end_ms": 2000, "reason": "muted"}])
    lines = folder.live_path.read_text(encoding="utf-8").splitlines()[:-1]  # no meeting_end: it crashed
    folder.live_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if audio_s is not None:
        buf = FlacBuffer(folder.audio_file("mic"))
        buf.write(np.zeros(int(audio_s * SAMPLE_RATE), np.float32))
        buf.close()
    return folder


def test_recovery_closes_an_open_mute_at_the_end_of_the_audio(tmp_path):
    folder = crashed_while_muted(tmp_path, audio_s=45.5)
    assert housekeeping.recover(tmp_path, None) == [folder.meeting_id]
    meta = folder.read_meta()
    assert meta["mic_muted_since_ms"] is None and meta["status"] == "interrupted" and meta["clean_exit"] is False
    assert meta["gaps"][-1] == {"source": "mic", "start_ms": 20_000, "end_ms": 45_500, "reason": "muted"}
    assert len(meta["gaps"]) == 2  # the earlier closed mute is kept
    assert meta["marks"] == 0


def test_recovery_without_audio_uses_the_last_line_and_leaves_closed_meetings_alone(tmp_path):
    folder = crashed_while_muted(tmp_path, audio_s=None, last_line_end=41_000)
    housekeeping.recover(tmp_path, None)
    assert folder.read_meta()["gaps"][-1]["end_ms"] == 41_000 and folder.read_meta()["mic_muted_since_ms"] is None
    # nothing open: nothing added
    other = make_meeting(tmp_path / "o", [], status=mt.STATUS_RECORDING)
    other.update_meta(clean_exit=None, mic_muted_since_ms=None)
    housekeeping.recover(tmp_path / "o", None)
    assert other.read_meta()["gaps"] == []


def test_audio_length_of_closed_and_unclosed_flac(tmp_path):
    folder = make_meeting(tmp_path, [])
    buf = FlacBuffer(folder.audio_file("mic"))
    buf.write(np.full(SAMPLE_RATE * 3, 0.1, np.float32))
    # not closed, like a crash: the header has no length. What the writer already flushed is counted.
    unclosed = housekeeping.audio_length_ms(folder)
    assert unclosed is not None and 0 <= unclosed <= 3000
    buf.close()
    assert housekeeping.audio_length_ms(folder) == 3000
    assert housekeeping.audio_length_ms(make_meeting(tmp_path / "n", [])) is None


def test_mute_gap_can_be_as_short_as_the_tail(tmp_path):
    rec, folder, cap = start(tmp_path)
    cap.push(tone(300, 0.5), silence(0.5))
    rec.set_mic_muted(True)
    rec.set_mic_muted(False)
    cap.push(silence(1.0), silence(1.0))
    rec.stop()
    (gap,) = folder.read_meta()["gaps"]
    assert gap["end_ms"] - gap["start_ms"] == 250 and gap["start_ms"] == 500
