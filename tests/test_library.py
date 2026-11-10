"""The Meetings list and deleting (WP6): scan and cache, status mapping, delete rules, meta after delete-audio."""

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import pytest

from huddle_helper import library, meeting as mt

from .helpers import STARTED_AT, make_meeting

LINES = [("mic", 0, 1000, "You", "hello."), ("system", 2000, 3000, "SPEAKER_01", "hi."),
         ("system", 4000, 5000, "SPEAKER_02", "yes.")]


def with_audio(folder, nbytes=1000):
    folder.audio_dir.mkdir(exist_ok=True)
    (folder.audio_dir / "mic.flac").write_bytes(b"x" * nbytes)
    return folder


def not_busy(_mid):
    return False


def fake_recycler(log):
    def recycle(paths):
        for p in paths:
            log.append(p)
            shutil.rmtree(p)
    return recycle


# -- scan ---------------------------------------------------------------------------------------
def test_scan_reads_rows_newest_first_and_counts_people_and_marks(tmp_path):
    marks = [{"type": "mark", "id": 1, "kind": "bookmark", "at_ms": 10},
             {"type": "mark", "id": 2, "kind": "action", "at_ms": 20}, {"type": "mark_delete", "id": 2}]
    a = make_meeting(tmp_path, LINES, final=LINES, title="Older", marks=marks)
    make_meeting(tmp_path, LINES[:1], title="Newer", started_at=STARTED_AT + timedelta(hours=2))
    infos = library.scan(tmp_path)
    assert [i.title for i in infos] == ["Newer", "Older"]
    older = infos[1]
    assert older.speakers == 3 and older.marks == 1 and older.done and older.status == mt.STATUS_COMPLETE
    assert infos[0].speakers is None and infos[0].marks == 0 and not infos[0].done
    # two labels named the same person count once
    a.write_speakers({"SPEAKER_01": "Dana", "SPEAKER_02": "Dana"})
    assert library.scan(tmp_path)[1].speakers == 2


def test_scan_skips_hidden_and_unreadable_folders(tmp_path):
    make_meeting(tmp_path, LINES, title="Good")
    (tmp_path / ".x.creating").mkdir()
    bad = make_meeting(tmp_path, LINES, title="Bad", started_at=STARTED_AT + timedelta(hours=1))
    bad.meta_path.write_text("{not json", encoding="utf-8")
    assert [i.title for i in library.scan(tmp_path)] == ["Good"]
    assert library.scan(tmp_path / "missing") == []


def test_cache_rereads_a_folder_only_when_something_feeding_its_row_changed(tmp_path):
    folder = make_meeting(tmp_path, LINES, final=LINES, title="Cached")
    cache = library.ScanCache()
    library.scan(tmp_path, cache)
    assert cache.reads == 1
    library.scan(tmp_path, cache)
    assert cache.reads == 1  # nothing changed
    # each file that feeds a row invalidates it
    folder.update_meta(title="Renamed")
    assert library.scan(tmp_path, cache)[0].title == "Renamed" and cache.reads == 2
    mt.append_jsonl(folder.marks_path, {"type": "mark", "id": 1, "kind": "bookmark", "at_ms": 5})
    assert library.scan(tmp_path, cache)[0].marks == 1 and cache.reads == 3
    folder.write_speakers({"SPEAKER_01": "Dana"})
    library.scan(tmp_path, cache)
    assert cache.reads == 4
    folder.done_path.unlink()
    assert library.scan(tmp_path, cache)[0].done is False and cache.reads == 5
    # a vanished folder leaves the list and the cache
    shutil.rmtree(folder.path)
    assert library.scan(tmp_path, cache) == []


def test_sizes_are_measured_on_a_worker_and_appear_in_a_later_scan(tmp_path):
    folder = with_audio(make_meeting(tmp_path, LINES, final=LINES, title="Sized"), 5000)
    cache = library.ScanCache()
    first = library.scan(tmp_path, cache)[0]
    assert first.audio_bytes in (None, 5000)  # the worker may already be done
    assert cache.wait_for_sizes()
    info = library.scan(tmp_path, cache)[0]
    assert info.audio_bytes == 5000
    assert info.total_bytes >= 5000 + folder.meta_path.stat().st_size
    assert library.totals([info]).measured
    # audio removed: the folder state changed, so the sizes are measured again
    shutil.rmtree(folder.audio_dir)
    folder.update_meta(audio_kept=False)
    library.scan(tmp_path, cache)
    assert cache.wait_for_sizes()
    assert library.scan(tmp_path, cache)[0].audio_bytes == 0


# -- status mapping -----------------------------------------------------------------------------------
def one(tmp_path, **kw):
    """(folder, MeetingInfo) of a single fixture meeting in its own folder."""
    status = kw.pop("status", mt.STATUS_COMPLETE)
    final = kw.pop("final", LINES)
    folder = make_meeting(tmp_path, LINES, final=final, status=status, **kw)
    if not any(folder.audio_dir.iterdir()):
        folder.audio_dir.rmdir()  # a finished meeting without kept audio has no audio folder
    return folder, library.scan(tmp_path)[0]


def test_status_mapping(tmp_path):
    cases = []
    f, i = one(tmp_path / "a")
    cases.append((i, None, library.COMPLETE, "Complete", False))
    f.update_meta(clean_exit=False)
    cases.append((library.scan(tmp_path / "a")[0], None, library.RECOVERED, "Interrupted (recovered)", False))
    f, i = one(tmp_path / "b", status=mt.STATUS_FAILED, final=None)
    f.update_meta(error="boom")
    i = library.scan(tmp_path / "b")[0]
    assert i.error == "boom"
    cases.append((i, None, library.FAILED, "Final pass failed", False))
    cases.append((i, library.LiveState(waiting=frozenset({i.id})), library.RETRY_WAITING,
                  "Retry queued until recording stops", True))
    cases.append((i, library.LiveState(queued=frozenset({i.id})), library.QUEUED, "Waiting for the final pass", True))
    cases.append((i, library.LiveState(queued=frozenset({i.id}), finalizing=i.id, progress=0.6), library.PROCESSING,
                  "Processing 60%", True))
    cases.append((i, library.LiveState(recording=i.id), library.RECORDING, "Recording", True))
    f, i = one(tmp_path / "c", status=mt.STATUS_RECORDING, final=None)
    cases.append((i, None, library.UNFINISHED, "Not finished", False))
    f, i = one(tmp_path / "d", status=mt.STATUS_INTERRUPTED, final=None)
    cases.append((i, None, library.UNFINISHED, "Interrupted", False))
    for info, live, kind, text, busy in cases:
        s = library.status_of(info, live)
        assert (s.kind, s.text, s.busy) == (kind, text, busy), (info.id, live)


def test_filter_and_search(tmp_path):
    _, done = one(tmp_path / "a", title="Weekly sync")
    _, failed = one(tmp_path / "b", title="Standup", status=mt.STATUS_FAILED, final=None)
    sd, sf = library.status_of(done), library.status_of(failed)
    assert library.matches(done, sd) and library.matches(failed, sf)
    assert library.matches(done, sd, status_filter="Complete") and not library.matches(failed, sf, status_filter="Complete")
    assert library.matches(failed, sf, status_filter="Final pass failed")
    assert library.matches(done, sd, search="  WEEKLY") and not library.matches(failed, sf, search="weekly")
    assert library.matches(done, sd, search=done.id[:10])  # the id works too
    assert set(library.FILTERS) >= {library.filter_name(sd), library.filter_name(sf)}


# -- delete ----------------------------------------------------------------------------------------------
def test_delete_meeting_goes_to_the_recycler_and_only_takes_the_meeting_folder(tmp_path):
    keep = make_meeting(tmp_path, LINES, title="Keep")
    gone = make_meeting(tmp_path, LINES, title="Gone", started_at=STARTED_AT + timedelta(hours=1))
    log = []
    how = library.delete_meeting(tmp_path, gone.meeting_id, is_busy=not_busy, recycler=fake_recycler(log),
                                 has_bin=lambda p: True)
    assert how == "recycled" and log == [gone.path] and not gone.path.exists() and keep.path.exists()


def test_delete_is_permanent_when_asked_or_when_there_is_no_recycle_bin(tmp_path):
    a = with_audio(make_meeting(tmp_path, LINES, title="A"))
    b = make_meeting(tmp_path, LINES, title="B", started_at=STARTED_AT + timedelta(hours=1))
    never = lambda paths: pytest.fail("must not use the Recycle Bin")  # noqa: E731
    assert library.delete_meeting(tmp_path, a.meeting_id, is_busy=not_busy, permanent=True, recycler=never,
                                  has_bin=lambda p: True) == "deleted" and not a.path.exists()
    assert library.delete_meeting(tmp_path, b.meeting_id, is_busy=not_busy, recycler=never,
                                  has_bin=lambda p: False) == "deleted" and not b.path.exists()


def test_delete_is_refused_while_the_meeting_is_busy(tmp_path):
    folder = with_audio(make_meeting(tmp_path, LINES, title="Busy"))
    boom = lambda paths: pytest.fail("nothing may be deleted")  # noqa: E731
    for op in (library.delete_meeting, library.delete_audio):
        with pytest.raises(library.MeetingBusy):
            op(tmp_path, folder.meeting_id, is_busy=lambda mid: mid == folder.meeting_id, recycler=boom,
               has_bin=lambda p: True)
    assert folder.path.exists() and folder.audio_dir.exists() and folder.read_meta().get("audio_deleted_at") is None


def test_delete_refuses_anything_that_is_not_a_meeting_folder_inside_the_meetings_folder(tmp_path):
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    outside = make_meeting(tmp_path, LINES, title="Outside")  # a meeting, but not inside `meetings`
    (meetings / "plain").mkdir()  # no meta.json
    (meetings / ".hidden").mkdir()
    for bad in ("..", ".", "", "plain", ".hidden", "missing", "../" + outside.meeting_id, outside.meeting_id,
                str(outside.path)):
        with pytest.raises(library.DeleteRefused):
            library.delete_meeting(meetings, bad, is_busy=not_busy, recycler=lambda p: pytest.fail(str(p)),
                                   has_bin=lambda p: True)
    assert outside.path.exists() and (meetings / "plain").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows")
def test_delete_refuses_a_junction_instead_of_following_it(tmp_path):
    import subprocess

    meetings = tmp_path / "meetings"
    meetings.mkdir()
    real = make_meeting(tmp_path, LINES, title="Real")
    link = meetings / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real.path)], check=True, capture_output=True)
    try:
        with pytest.raises(library.DeleteRefused):
            library.delete_meeting(meetings, "link", is_busy=not_busy, permanent=True, has_bin=lambda p: True)
        assert (real.path / mt.META).exists()
    finally:
        os.rmdir(link)  # removes the junction only


def test_delete_audio_sets_the_meta_fields_and_keeps_the_transcripts(tmp_path):
    folder = with_audio(make_meeting(tmp_path, LINES, final=LINES, title="Audio"))
    folder.update_meta(audio_kept=True)
    log = []
    before = mt.now_local().replace(microsecond=0)
    how = library.delete_audio(tmp_path, folder.meeting_id, is_busy=not_busy, recycler=fake_recycler(log),
                               has_bin=lambda p: True)
    assert how == "recycled" and log == [folder.audio_dir]
    meta = folder.read_meta()
    assert meta["audio_kept"] is False and meta["status"] == mt.STATUS_COMPLETE
    assert datetime.fromisoformat(meta["audio_deleted_at"]) >= before
    assert folder.final_path.exists() and folder.live_path.exists() and folder.is_done and not folder.audio_dir.exists()
    # no audio folder: nothing to do, meta untouched
    stamp = meta["audio_deleted_at"]
    assert library.delete_audio(tmp_path, folder.meeting_id, is_busy=not_busy, recycler=fake_recycler(log),
                                has_bin=lambda p: True) == "none"
    assert folder.read_meta()["audio_deleted_at"] == stamp
    # permanent
    other = with_audio(make_meeting(tmp_path, LINES, title="Perm", started_at=STARTED_AT + timedelta(hours=1)))
    assert library.delete_audio(tmp_path, other.meeting_id, is_busy=not_busy, permanent=True,
                                has_bin=lambda p: True) == "deleted" and not other.audio_dir.exists()
    assert other.read_meta()["audio_kept"] is False


def test_failed_recycle_leaves_the_meeting_and_its_meta_alone(tmp_path):
    folder = with_audio(make_meeting(tmp_path, LINES, title="Stays"))

    def fail(paths):
        raise OSError("bin unavailable")

    with pytest.raises(OSError):
        library.delete_audio(tmp_path, folder.meeting_id, is_busy=not_busy, recycler=fail, has_bin=lambda p: True)
    assert folder.audio_dir.exists() and "audio_deleted_at" not in folder.read_meta()


def test_finished_within_and_hours_since():
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def mk(at, done=True):
        return library.MeetingInfo("m", None, "t", "", None, "complete", done, False, None, 0, False, None, None, "", at)

    assert library.finished_within(mk("2026-01-15T00:30:00+00:00"), now=now)
    assert not library.finished_within(mk("2026-01-14T11:00:00+00:00"), now=now)
    assert not library.finished_within(mk(""), now=now)
    assert not library.finished_within(mk("2026-01-15T11:00:00+00:00", False), now=now)
    assert library.hours_since("2026-01-15T10:00:00+00:00", now) == 2.0 and library.hours_since("junk", now) is None
