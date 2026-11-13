"""Recover never touches a meeting another process holds (#63, ADR 0002): a fresh heartbeat means a
process holds the meeting (recording it, running its final pass, or holding it in its final pass queue),
and recover skips it whatever its status. A meeting recover does take has its shared mic closed the way
Stop closes it."""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from huddle_helper import app, config, final_pass as fp, heartbeat as hb, housekeeping, meeting as mt, status

from .conftest import FakeDiarizer, make_meeting_to_finalize
from .helpers import STARTED_AT, make_meeting, no_wait, stale_heartbeat
from .test_library import LINES

FAST = 0.05  # the tray's heartbeat interval in these tests (the app uses hb.INTERVAL_S)


class BlockingDiarizer(FakeDiarizer):
    """Holds the final pass inside its diarization until ``release`` is set."""

    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered, self.release = entered, release

    def diarize(self, audio, progress=None):
        self.entered.set()
        assert self.release.wait(10), "test never released the final pass"
        return super().diarize(audio, progress)


@pytest.fixture()
def blocked_tray(tmp_path):
    """A tray whose final pass, once it starts on a meeting, waits inside diarization for ``release``."""
    entered, release = threading.Event(), threading.Event()
    runs = []

    def diarizer():
        runs.append(1)
        return BlockingDiarizer(entered, release)

    pass_ = fp.FinalPass(config.Settings(), diarizer_factory=diarizer, embedder_factory=None,
                         transcriber_factory=None, heartbeat_s=FAST)
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)}, final_pass=pass_,
                            heartbeats=hb.Heartbeats(FAST))
    yield ctl, entered, release, runs
    release.set()
    ctl.shutdown()


def stopped_meeting(tmp_path):
    """A meeting Stop has left for its final pass, whose heartbeat stopped long ago (as Stop leaves it)."""
    folder = make_meeting_to_finalize(tmp_path)
    stale_heartbeat(folder)
    return folder


def test_cli_recover_while_the_tray_runs_a_final_pass_leaves_the_meeting_to_the_tray(tmp_path, blocked_tray):
    ctl, entered, release, runs = blocked_tray
    folder = stopped_meeting(tmp_path)
    ctl._enqueue(folder)
    assert entered.wait(10)

    finalized = []
    assert housekeeping.recover(tmp_path, finalized.append, wait=no_wait) == []
    assert finalized == []

    release.set()
    folder_done = _wait_until(lambda: folder.is_done)
    assert folder_done and runs == [1]
    assert folder.read_meta()["status"] == mt.STATUS_COMPLETE


def test_a_meeting_queued_behind_the_running_one_is_skipped_by_cli_recover(tmp_path, blocked_tray):
    ctl, entered, release, runs = blocked_tray
    running, queued = stopped_meeting(tmp_path), stopped_meeting(tmp_path)
    ctl._enqueue(running)
    assert entered.wait(10)
    ctl._enqueue(queued)

    finalized = []
    assert housekeeping.recover(tmp_path, finalized.append, wait=no_wait) == []
    assert finalized == []

    release.set()
    assert _wait_until(lambda: running.is_done and queued.is_done) and runs == [1, 1]


def test_a_retry_waiting_for_the_recording_to_stop_keeps_a_fresh_heartbeat(tmp_path, blocked_tray):
    ctl = blocked_tray[0]
    failed = make_meeting(tmp_path, LINES, status=mt.STATUS_FAILED, title="Failed")
    stale_heartbeat(failed)
    recording = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING, title="Live",
                             started_at=STARTED_AT + timedelta(hours=1))
    ctl._recorder = SimpleNamespace(folder=recording, stop=lambda: recording)
    ctl.state.phase = "recording"
    assert ctl.retry_final(failed.meeting_id) == "queued"
    assert status.read(failed).heartbeat_fresh()


def test_once_the_tray_exits_its_meetings_go_stale_and_the_next_recover_handles_them(tmp_path, blocked_tray):
    ctl, entered, release, runs = blocked_tray
    running, queued = stopped_meeting(tmp_path), stopped_meeting(tmp_path)
    ctl._enqueue(running)
    assert entered.wait(10)
    ctl._enqueue(queued)
    assert housekeeping.recover(tmp_path, None, wait=no_wait) == []

    ctl.shutdown()  # the tray exits; its final pass thread is a daemon and dies with the process
    for folder in (running, queued):
        stale_heartbeat(folder)  # 15 s on, as nothing beats for them any more
    time.sleep(5 * FAST)  # several of the tray's beat intervals: nothing beats again
    assert not status.read(queued).heartbeat_fresh()
    assert sorted(housekeeping.recover(tmp_path, None, wait=no_wait)) == sorted([queued.meeting_id])
    # the running one is left: its final pass still beats here (in the tray it dies with the process)


def _wait_until(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# -- shared mic in a recovered meeting -------------------------------------------------------------
def test_recover_closes_an_open_shared_mic_stretch_at_the_last_lines_end_as_stop_does(tmp_path):
    from huddle_helper.transcript_model import DividerRow, shared_mic_dividers

    lines = [("mic", 100, 900, "You", "Hello."), ("system", 500, 1500, "SPEAKER_01", "First."),
             ("system", 2500, 3000, "SPEAKER_01", "Second.")]
    folder = make_meeting(tmp_path, lines, status=mt.STATUS_RECORDING)
    folder.update_meta(clean_exit=None, shared_mic=[[0, 50], [100, None]])  # the recorder died mid-stretch
    stale_heartbeat(folder)

    assert housekeeping.recover(tmp_path, None, wait=no_wait) == [folder.meeting_id]
    meta = folder.read_meta()
    assert meta["shared_mic"] == [[100, 3000]]  # no mic line in 0-50: dropped
    assert meta["ended_ms"] == 3000
    assert meta["status"] == mt.STATUS_INTERRUPTED
    assert shared_mic_dividers(meta) == [DividerRow(100, True)]  # no "Mic is just you from here"


def test_recover_holds_the_meetings_waiting_their_turn_while_it_finalizes_one(tmp_path):
    """A second process looking while this recover is busy with the first meeting finds them all held."""
    a, b = stopped_meeting(tmp_path), stopped_meeting(tmp_path)
    fresh_while_finalizing = []

    def finalize(folder):
        fresh_while_finalizing.append(sorted(f.meeting_id for f in (a, b) if status.read(f).heartbeat_fresh()))

    assert sorted(housekeeping.recover(tmp_path, finalize, wait=no_wait)) == sorted([a.meeting_id, b.meeting_id])
    assert fresh_while_finalizing == [sorted([a.meeting_id, b.meeting_id])] * 2
