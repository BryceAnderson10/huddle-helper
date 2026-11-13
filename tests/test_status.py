"""A meeting's status (#62): from its files, and with the app's in-process state laid over it."""

from datetime import timedelta

import pytest

from huddle_helper import heartbeat as hb, library, meeting as mt, status

from .helpers import make_meeting


def recording_meeting(tmp_path, beat_age_s: float | None):
    """Status ``recording`` with its last beat ``beat_age_s`` old (None: no heartbeat file)."""
    folder = mt.MeetingFolder.create(tmp_path / "meetings", "Made Up Sync", mt.now_local(), {})
    if beat_age_s is None:
        folder.heartbeat_path.unlink()
    else:
        folder.beat(mt.now_local() - timedelta(seconds=beat_age_s))
    return folder


# -- from the files ----------------------------------------------------------------------------------

def test_recording_with_a_fresh_heartbeat_is_recording(tmp_path):
    assert status.read(recording_meeting(tmp_path, 1)).status() == status.RECORDING


@pytest.mark.parametrize("beat_age_s", [hb.STALE_AFTER_S + 1, -(hb.STALE_AFTER_S + 1), None])
def test_recording_with_a_stale_future_or_missing_heartbeat_is_recorder_died(tmp_path, beat_age_s):
    assert status.read(recording_meeting(tmp_path, beat_age_s)).status() == status.RECORDER_DIED


def test_an_unreadable_heartbeat_is_recorder_died(tmp_path):
    folder = recording_meeting(tmp_path, 1)
    folder.heartbeat_path.write_text("{not json", encoding="utf-8")
    assert status.read(folder).status() == status.RECORDER_DIED


def test_the_heartbeat_ages_against_the_given_clock(tmp_path):
    files = status.read(recording_meeting(tmp_path, 0))
    later = mt.now_local() + timedelta(seconds=hb.STALE_AFTER_S + 5)
    assert files.status(later) == status.RECORDER_DIED


@pytest.mark.parametrize("meta_status", ["processing", "complete", "interrupted", "failed", "something-new"])
def test_any_other_status_is_the_meta_status(tmp_path, meta_status):
    folder = make_meeting(tmp_path, [], status=meta_status)
    folder.beat()  # a fresh beat does not make a stopped meeting recording
    assert status.read(folder).status() == meta_status


def test_a_missing_status_is_unknown(tmp_path):
    folder = make_meeting(tmp_path, [])
    folder.drop_meta("status")
    assert status.read(folder).status() == status.UNKNOWN


def test_done_does_not_change_the_file_status(tmp_path):
    # the context command reports meta.json's status as it is (docs/OUTPUT_SPEC.md, meeting.state)
    folder = make_meeting(tmp_path, [], final=[], status="interrupted")
    files = status.read(folder)
    assert files.done and files.status() == status.INTERRUPTED


def test_a_fresh_heartbeat_is_seen_whatever_the_status(tmp_path):
    # what recover needs (ADR 0002): a fresh heartbeat means a process holds the meeting
    folder = make_meeting(tmp_path, [], status="processing")
    folder.heartbeat_path.unlink(missing_ok=True)
    assert not status.read(folder).heartbeat_fresh()
    folder.beat()
    assert status.read(folder).heartbeat_fresh()


def test_read_raises_when_meta_cannot_be_read(tmp_path):
    folder = make_meeting(tmp_path, [])
    folder.meta_path.write_text("{half", encoding="utf-8")
    with pytest.raises(ValueError):
        status.read(folder)


def test_read_can_take_meta_already_read(tmp_path):
    folder = recording_meeting(tmp_path, 1)
    assert status.read(folder, {"status": "failed"}).status() == status.FAILED


# -- recovered ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("meta, expected", [
    ({"status": "interrupted", "clean_exit": False}, True),
    ({"status": "complete", "clean_exit": False}, True),  # recovered, then its final pass finished
    ({"status": "interrupted"}, True),
    ({"status": "complete", "clean_exit": True}, False),
    ({"status": "recording", "clean_exit": None}, False),
    ({"status": "failed", "clean_exit": True}, False),
])
def test_recovered_means_interrupted_or_clean_exit_false(meta, expected):
    assert status.recovered(meta) is expected


def test_files_carry_recovered(tmp_path):
    folder = make_meeting(tmp_path, [], final=[])
    assert not status.read(folder).recovered
    folder.update_meta(clean_exit=False)
    assert status.read(folder).recovered


# -- with the app's in-process state ----------------------------------------------------------------------

def files(meta_status="recording", done=False, clean_exit=None, beat_age_s=1.0):
    beat = None if beat_age_s is None else mt.now_local() - timedelta(seconds=beat_age_s)
    return status.Files(meta_status=meta_status, clean_exit=clean_exit, done=done, last_beat=beat)


def shown(f, live=None):
    return status.shown(f, "m", live)


def test_with_nothing_held_by_this_app_the_file_status_shows():
    assert shown(files()) == status.Shown(status.RECORDING, here=False, recovered=False)
    assert shown(files(beat_age_s=None)).status == status.RECORDER_DIED
    assert shown(files("failed")).status == status.FAILED
    assert shown(files("processing"), library.LiveState()).status == status.PROCESSING


def test_done_shows_as_complete_whatever_the_meta_status():
    s = shown(files("interrupted", done=True, clean_exit=False))
    assert s == status.Shown(status.COMPLETE, here=False, recovered=True)


@pytest.mark.parametrize("live, expected", [
    (library.LiveState(recording="m"), status.RECORDING),
    (library.LiveState(recording="m", stopping=True), status.STOPPING),
    (library.LiveState(finalizing="m", queued=frozenset({"m"})), status.PROCESSING),
    (library.LiveState(waiting=frozenset({"m"})), status.RETRY_WAITING),
    (library.LiveState(queued=frozenset({"m"})), status.QUEUED),
])
def test_what_the_app_is_doing_wins_over_the_files(live, expected):
    # meta.json lags the app by a moment: a meeting this app has stopped may still say recording, and a
    # retried one still says failed
    for f in (files("recording", beat_age_s=None), files("failed"), files("processing")):
        assert shown(f, live) == status.Shown(expected, here=True, recovered=False)


def test_the_app_state_of_another_meeting_does_not_count():
    live = library.LiveState(recording="other", finalizing="other2", queued=frozenset({"other2"}))
    assert shown(files("failed"), live) == status.Shown(status.FAILED, here=False, recovered=False)


def test_recovered_is_carried_through_the_overlay():
    assert shown(files("interrupted", clean_exit=False), library.LiveState(queued=frozenset({"m"}))).recovered
