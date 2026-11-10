"""AppController side of the Meetings view (WP6): is_busy, retry_final, delete_meetings."""

import functools
import queue
import shutil
from datetime import timedelta
from types import SimpleNamespace

import pytest

from huddle_helper import app, library, meeting as mt

from .helpers import STARTED_AT, make_meeting
from .test_library import LINES, with_audio


@pytest.fixture()
def ctl(tmp_path):
    c = app.AppController(overrides={"meetings_dir": str(tmp_path)})
    c._jobs = queue.Queue()  # the real worker stays blocked on the old queue: nothing is finalized here
    return c


def failed_meeting(tmp_path, title="Failed", hours=0, error="boom"):
    folder = make_meeting(tmp_path, LINES, status=mt.STATUS_FAILED, title=title,
                          started_at=STARTED_AT + timedelta(hours=hours))
    folder.update_meta(error=error)
    return folder


def test_retry_clears_the_error_and_queues_the_job(ctl, tmp_path):
    folder = failed_meeting(tmp_path)
    assert not ctl.is_busy(folder.meeting_id)
    assert ctl.retry_final(folder.meeting_id) == "started"
    assert "error" not in folder.read_meta()
    queued = ctl._jobs.get_nowait()
    assert queued.path == folder.path and ctl.state.finalize_queue == 1
    assert ctl.is_busy(folder.meeting_id) and folder.meeting_id in ctl.live_state().queued
    # a second press while it is queued does nothing
    assert ctl.retry_final(folder.meeting_id) == "refused" and ctl._jobs.empty()


def test_retry_is_refused_for_meetings_that_are_not_failed(ctl, tmp_path):
    done = make_meeting(tmp_path, LINES, final=LINES, title="Done")
    rec = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING, title="Rec", started_at=STARTED_AT + timedelta(hours=1))
    for mid in (done.meeting_id, rec.meeting_id, "nope", "..", ""):
        assert ctl.retry_final(mid) == "refused"
    assert ctl._jobs.empty() and ctl.state.finalize_queue == 0


def test_retry_during_a_recording_waits_and_is_released_when_stop_completes(ctl, tmp_path):
    failed = failed_meeting(tmp_path)
    recording = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING, title="Live",
                             started_at=STARTED_AT + timedelta(hours=1))
    ctl._recorder = SimpleNamespace(folder=recording, stop=lambda: recording)
    ctl._live_ids.add(recording.meeting_id)
    ctl.state.phase = "recording"
    assert ctl.is_busy(recording.meeting_id)
    assert ctl.retry_final(failed.meeting_id) == "queued"
    assert ctl._jobs.empty() and ctl.state.finalize_queue == 0
    assert failed.read_meta()["error"] == "boom"  # still shown while it waits
    state = ctl.live_state()
    assert state.waiting == {failed.meeting_id} and state.recording == recording.meeting_id
    assert ctl.is_busy(failed.meeting_id)  # cannot be deleted while it waits
    assert ctl.retry_final(failed.meeting_id) == "refused"  # one retry is enough
    ctl.stop(wait=True)  # Stop completes: the meeting's own pass first, then the retry
    first, second = ctl._jobs.get_nowait(), ctl._jobs.get_nowait()
    assert (first.meeting_id, second.meeting_id) == (recording.meeting_id, failed.meeting_id)
    assert "error" not in failed.read_meta() and ctl.state.phase == "ready"
    assert ctl.live_state().waiting == frozenset() and ctl.state.finalize_queue == 2


def test_delete_meetings_refuses_busy_ones_and_deletes_the_rest(ctl, tmp_path, monkeypatch):
    a = with_audio(make_meeting(tmp_path, LINES, final=LINES, title="A"))
    b = make_meeting(tmp_path, LINES, final=LINES, title="B", started_at=STARTED_AT + timedelta(hours=1))
    c = make_meeting(tmp_path, LINES, final=LINES, title="C", started_at=STARTED_AT + timedelta(hours=2))
    ctl._queued.add(b.meeting_id)  # queued for the final pass
    # use the real library functions, with a recycler that deletes (never the real Recycle Bin in a test)
    real_meeting, real_audio = library.delete_meeting, library.delete_audio
    kw = dict(recycler=lambda paths: [shutil.rmtree(p) for p in paths], has_bin=lambda p: True)
    monkeypatch.setattr(library, "delete_meeting", functools.partial(real_meeting, **kw))
    monkeypatch.setattr(library, "delete_audio", functools.partial(real_audio, **kw))
    res = ctl.delete_meetings([a.meeting_id, b.meeting_id, c.meeting_id, "missing"])
    assert res.done == [a.meeting_id, c.meeting_id] and res.refused == [b.meeting_id, "missing"]
    assert res.how == "recycled" and b.path.exists() and not a.path.exists() and not c.path.exists()
    # audio only: a meeting without audio is skipped, not an error
    d = with_audio(make_meeting(tmp_path, LINES, final=LINES, title="D", started_at=STARTED_AT + timedelta(hours=3)))
    res = ctl.delete_meetings([d.meeting_id, b.meeting_id], audio_only=True)
    assert res.refused == [b.meeting_id] and res.done == [d.meeting_id] and not d.audio_dir.exists()
    assert d.read_meta()["audio_kept"] is False
    res = ctl.delete_meetings([d.meeting_id], audio_only=True)
    assert res.skipped == [d.meeting_id] and res.done == []
    assert ctl.delete_meetings([]).ok


def test_a_meeting_being_recorded_is_busy_and_cannot_be_deleted(ctl, tmp_path):
    rec = make_meeting(tmp_path, LINES, status=mt.STATUS_RECORDING, title="Live")
    ctl._recorder = SimpleNamespace(folder=rec)
    ctl.state.phase = "recording"
    assert ctl.is_busy(rec.meeting_id)
    res = ctl.delete_meetings([rec.meeting_id], permanent=True)
    assert res.refused == [rec.meeting_id] and rec.path.exists()
