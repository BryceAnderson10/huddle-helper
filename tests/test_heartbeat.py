"""Recorder heartbeat (#40): a reader tells "no active meeting" from "recording but quiet" from
"recorder died" by meta.json status plus the freshness of heartbeat.json, never by transcript activity."""

import json
import time
from datetime import timedelta

from huddle_helper import config, heartbeat as hb, meeting as mt, status
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber
from .helpers import ManualCapture

FAST = 0.05  # heartbeat interval for the tests (the app uses hb.INTERVAL_S)


def liveness(folder, now=None):
    """The heartbeat's reading of the meeting (the context command's ``heartbeat.state``)."""
    return status.heartbeat_state(status.read(folder).status(now))


def start(tmp_path, **kw):
    holder = {}

    def factory(**fkw):
        holder["cap"] = ManualCapture(fkw["on_audio"])
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer,
                       capture_factory=factory)
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "meetings")), comps, heartbeat_s=FAST)
    return rec, rec.start("Heartbeat Test", **kw)


def wait_for_beat_after(folder, after, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        beat = hb.last_beat(folder)
        if beat is not None and beat > after:
            return beat
        time.sleep(FAST / 2)
    raise AssertionError(f"heartbeat did not advance past {after}")


def crashed_meeting(tmp_path, beat_age_s: float | None):
    """A meeting left at status ``recording`` whose last heartbeat is ``beat_age_s`` old (None: no file)."""
    folder = mt.MeetingFolder.create(tmp_path / "meetings", "Made Up Sync", mt.now_local(), {})
    if beat_age_s is None:
        folder.heartbeat_path.unlink()
    else:
        folder.beat(mt.now_local() - timedelta(seconds=beat_age_s))
    return folder


# -- recording but quiet ---------------------------------------------------------------------------

def test_heartbeat_advances_in_total_silence_and_the_meeting_is_active(tmp_path):
    rec, folder = start(tmp_path)
    try:
        first = hb.last_beat(folder)
        assert first is not None  # in place from the moment the folder appears
        second = wait_for_beat_after(folder, first)
        wait_for_beat_after(folder, second)
        # nothing was said, no audio arrived at all: the meeting is still healthy
        assert [e["type"] for e in read_events(folder.live_path)] == ["meeting_start"]
        assert liveness(folder) == hb.ACTIVE
    finally:
        rec.stop()


def test_heartbeat_advances_while_the_mic_is_muted(tmp_path):
    rec, folder = start(tmp_path, start_muted=True)
    try:
        assert folder.read_meta()["mic_muted_since_ms"] == 0
        wait_for_beat_after(folder, wait_for_beat_after(folder, hb.last_beat(folder)))
        assert liveness(folder) == hb.ACTIVE
    finally:
        rec.stop()


def test_a_quiet_meeting_with_a_fresh_heartbeat_is_active_however_old_its_last_line(tmp_path):
    folder = crashed_meeting(tmp_path, beat_age_s=1)
    folder.update_meta(started_at=mt.iso(mt.now_local() - timedelta(hours=2)))
    assert liveness(folder) == hb.ACTIVE


# -- no active meeting -----------------------------------------------------------------------------

def test_after_stop_the_heartbeat_stops_and_the_meeting_is_not_recording(tmp_path):
    rec, folder = start(tmp_path)
    wait_for_beat_after(folder, hb.last_beat(folder))
    rec.stop()
    assert folder.read_meta()["status"] == mt.STATUS_PROCESSING
    last = hb.last_beat(folder)
    time.sleep(FAST * 6)
    assert hb.last_beat(folder) == last  # no thread is left beating
    assert liveness(folder) == hb.NOT_RECORDING
    assert liveness(folder, now=last + timedelta(minutes=5)) == hb.NOT_RECORDING


def test_finished_and_recovered_meetings_are_not_recording_whatever_the_heartbeat(tmp_path):
    for status in (mt.STATUS_PROCESSING, mt.STATUS_COMPLETE, mt.STATUS_INTERRUPTED, mt.STATUS_FAILED):
        folder = crashed_meeting(tmp_path, beat_age_s=0)
        folder.update_meta(status=status)
        assert liveness(folder) == hb.NOT_RECORDING, status


def test_a_start_that_fails_leaves_no_heartbeat_thread(tmp_path):
    class BrokenCapture(ManualCapture):
        def start(self, t0):
            raise OSError("no device")

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(), scorer_factory=EnergyScorer,
                       capture_factory=lambda **kw: BrokenCapture(kw["on_audio"]))
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path / "meetings")), comps, heartbeat_s=FAST)
    try:
        rec.start("Heartbeat Test")
    except OSError:
        pass
    else:
        raise AssertionError("start should have failed")
    (folder,) = mt.iter_meetings(tmp_path / "meetings")
    last = hb.last_beat(folder)
    time.sleep(FAST * 6)
    assert hb.last_beat(folder) == last and rec._heartbeat is None
    assert liveness(folder, now=last + timedelta(seconds=hb.STALE_AFTER_S + 1)) == hb.RECORDER_DIED


# -- recorder died ---------------------------------------------------------------------------------

def test_a_crash_leaves_status_recording_with_a_stale_heartbeat(tmp_path):
    rec, folder = start(tmp_path)
    wait_for_beat_after(folder, hb.last_beat(folder))
    rec._heartbeat.stop()  # what a crash does to the beating thread; meta.json is never touched
    try:
        last = hb.last_beat(folder)
        assert folder.read_meta()["status"] == mt.STATUS_RECORDING
        assert liveness(folder, now=last + timedelta(seconds=hb.STALE_AFTER_S - 1)) == hb.ACTIVE
        assert liveness(folder, now=last + timedelta(seconds=hb.STALE_AFTER_S + 1)) == hb.RECORDER_DIED
    finally:
        rec.stop()


def test_a_stale_heartbeat_is_never_active_however_recent_the_meeting(tmp_path):
    folder = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 5)
    assert folder.read_meta()["started_at"] >= mt.iso(mt.now_local() - timedelta(minutes=1))
    assert liveness(folder) == hb.RECORDER_DIED


def test_a_missing_or_unreadable_heartbeat_means_the_recorder_died(tmp_path):
    assert liveness(crashed_meeting(tmp_path, beat_age_s=None)) == hb.RECORDER_DIED
    folder = crashed_meeting(tmp_path, beat_age_s=0)
    folder.heartbeat_path.write_text("{not json", encoding="utf-8")
    assert hb.last_beat(folder) is None
    assert liveness(folder) == hb.RECORDER_DIED


def test_a_heartbeat_from_the_future_is_stale_too(tmp_path):
    """A clock set back after a crash must not keep a dead meeting looking alive."""
    folder = crashed_meeting(tmp_path, beat_age_s=-(hb.STALE_AFTER_S + 60))
    assert liveness(folder) == hb.RECORDER_DIED


# -- the file ------------------------------------------------------------------------------------

def test_heartbeat_file_is_small_json_with_a_wall_clock_time(tmp_path):
    folder = crashed_meeting(tmp_path, beat_age_s=0)
    data = json.loads(folder.heartbeat_path.read_text(encoding="utf-8"))
    assert set(data) == {"at"}
    assert hb.last_beat(folder).utcoffset() is not None
    assert not folder.heartbeat_path.with_name(mt.HEARTBEAT + ".tmp").exists()
