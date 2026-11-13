"""Recover (#59) leaves a meeting that is still recording alone: a ``recording`` meeting with a fresh
heartbeat is skipped whoever runs recover, and a stale heartbeat is asked about again once, one beat
interval later, before the meeting is treated as one whose recorder died."""

from datetime import timedelta

from huddle_helper import heartbeat as hb, housekeeping, meeting as mt
from huddle_helper.writer import read_events

from .test_heartbeat import crashed_meeting, start, wait_for_beat_after


class Waits:
    """Stands in for the re-check's sleep: records each wait and runs ``then`` once, instead of sleeping."""

    def __init__(self, then=None):
        self.calls: list[float] = []
        self.then = then

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.then is not None:
            self.then()


def test_recover_without_the_skip_list_leaves_a_meeting_the_recorder_is_writing_alone(tmp_path):
    rec, folder = start(tmp_path)
    try:
        wait_for_beat_after(folder, hb.last_beat(folder))
        live_before = folder.live_path.read_bytes()
        finalized = []
        waits = Waits()
        assert housekeeping.recover(tmp_path / "meetings", finalized.append, wait=waits) == []
        assert finalized == [] and waits.calls == []  # nothing to recover: no wait either
        assert folder.live_path.read_bytes() == live_before
        assert [e["type"] for e in read_events(folder.live_path)] == ["meeting_start"]
        assert folder.read_meta()["status"] == mt.STATUS_RECORDING
    finally:
        rec.stop()


def test_a_recording_meeting_still_stale_after_the_recheck_is_recovered(tmp_path):
    folder = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 5)
    waits = Waits()
    finalized = []
    assert housekeeping.recover(tmp_path / "meetings", finalized.append, wait=waits) == [folder.meeting_id]
    assert waits.calls == [hb.INTERVAL_S]
    assert [f.meeting_id for f in finalized] == [folder.meeting_id]
    assert folder.read_meta()["status"] == mt.STATUS_INTERRUPTED
    assert read_events(folder.live_path)[-1]["type"] == "meeting_end"


def test_a_heartbeat_that_beats_again_within_the_recheck_is_left_alone(tmp_path):
    """The PC woke from sleep: the beat was stale when recover first looked, fresh one interval later."""
    folder = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 60)
    waits = Waits(then=folder.beat)
    assert housekeeping.recover(tmp_path / "meetings", None, wait=waits) == []
    assert waits.calls == [hb.INTERVAL_S]
    assert folder.read_meta()["status"] == mt.STATUS_RECORDING
    assert not folder.live_path.exists()  # recover never wrote a meeting_end into it


def test_several_stale_meetings_share_one_recheck(tmp_path):
    a = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 5)
    b = crashed_meeting(tmp_path, beat_age_s=None)
    waits = Waits()
    assert sorted(housekeeping.recover(tmp_path / "meetings", None, wait=waits)) == sorted([a.meeting_id, b.meeting_id])
    assert waits.calls == [hb.INTERVAL_S]


def test_meetings_not_recording_are_recovered_without_a_wait(tmp_path):
    folder = crashed_meeting(tmp_path, beat_age_s=0)  # a fresh beat does not matter once it is processing
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)
    waits = Waits()
    finalized = []
    assert housekeeping.recover(tmp_path / "meetings", finalized.append, wait=waits) == [folder.meeting_id]
    assert waits.calls == [] and [f.meeting_id for f in finalized] == [folder.meeting_id]


def test_the_in_process_skip_list_still_wins_over_a_stale_heartbeat(tmp_path):
    """Recover at app start passes the meetings it holds; those are skipped whatever their heartbeat."""
    folder = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 5)
    waits = Waits()
    assert housekeeping.recover(tmp_path / "meetings", None, skip={folder.meeting_id}, wait=waits) == []
    assert waits.calls == [] and folder.read_meta()["status"] == mt.STATUS_RECORDING


def test_a_meeting_stopped_during_the_recheck_is_left_to_the_process_that_stopped_it(tmp_path):
    """Its recorder looked dead, but by the re-check it had moved on to its final pass: alive after all."""
    folder = crashed_meeting(tmp_path, beat_age_s=hb.STALE_AFTER_S + 5)
    waits = Waits(then=lambda: folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True))
    finalized = []
    assert housekeeping.recover(tmp_path / "meetings", finalized.append, wait=waits) == []
    assert finalized == [] and folder.read_meta()["status"] == mt.STATUS_PROCESSING
