"""Meetings view logic (WP6): the delete confirmation wording and which buttons are on. No Tk."""

import pytest

from huddle_helper import library, meeting as mt
from huddle_helper.gui import confirm, meetings_logic as ml
from huddle_helper.gui.confirm import Item, delete_view

from .helpers import make_meeting
from .test_library import LINES, one, with_audio

MB = 1024 * 1024


def old(title, mb=10):
    return Item(title, mb * MB, finished_hours=100.0)


# -- the confirmation wording ---------------------------------------------------------------------------
def test_the_wording_of_the_spec_example():
    v = delete_view([old("test-2-2", 21), old("test-3", 12)])
    assert v.heading == "Move 2 meetings (test-2-2, test-3), 33 MB, to the Recycle Bin?"
    assert v.details[0].startswith("They can be restored from there until the Recycle Bin is emptied.")
    assert "Until then the transcript and audio stay on this PC." in v.details[0]
    assert not v.warnings and not v.permanent and v.can_choose and not v.cancel_default
    assert (v.title, v.confirm) == ("Delete meetings", "Move to Recycle Bin")


def test_one_meeting_says_it_not_they():
    v = delete_view([old("Standup", 3)])
    assert v.heading == "Move 1 meeting (Standup), 3.0 MB, to the Recycle Bin?"
    assert v.details[0].startswith("It can be restored")
    assert v.title == "Delete meeting"


def test_permanent_by_choice_says_it_cannot_be_undone_and_is_not_in_the_bin():
    v = delete_view([old("a", 21), old("b", 12)], permanent=True)
    assert v.heading == "Permanently delete 2 meetings (a, b), 33 MB?"
    assert v.details == ("This cannot be undone: they will not go to the Recycle Bin.",)
    assert v.permanent and v.can_choose and v.cancel_default and v.confirm == "Delete permanently"
    assert "restored" not in " ".join(v.details)


def test_no_recycle_bin_means_permanently_and_no_check_box():
    v = delete_view([old("a", 5)], has_bin=False)
    assert v.heading == "Permanently delete 1 meeting (a), 5.0 MB?"
    assert v.details == ("This drive has no Recycle Bin, so it cannot be restored.",)
    assert v.permanent and not v.can_choose and v.confirm == "Delete permanently"
    # a permanent choice changes nothing about it
    assert delete_view([old("a", 5)], has_bin=False, permanent=True) == v


def test_audio_only_names_the_audio_and_says_the_transcripts_stay():
    v = delete_view([old("a", 20), old("b", 10)], audio_only=True)
    assert v.heading == "Move the kept audio of 2 meetings (a, b), 30 MB, to the Recycle Bin?"
    assert v.details[0].startswith("The audio can be restored from there until the Recycle Bin is emptied.")
    assert "Until then the audio stays on this PC." in v.details[0]
    assert "transcripts and bookmarks stay" in v.details[1]
    assert (v.title, v.confirm) == ("Delete audio", "Move audio to Recycle Bin")
    p = delete_view([old("a", 20)], audio_only=True, permanent=True)
    assert p.heading == "Permanently delete the kept audio of 1 meeting (a), 20 MB?"
    assert p.details[0] == "This cannot be undone: the audio will not go to the Recycle Bin."
    assert p.confirm == "Delete audio permanently"
    n = delete_view([old("a", 20)], audio_only=True, has_bin=False)
    assert n.details[0] == "This drive has no Recycle Bin, so the audio cannot be restored."


def test_meetings_finished_in_the_last_24_hours_get_a_stronger_warning():
    v = delete_view([Item("Fresh", 4 * MB, finished_hours=3.2), Item("Older", 4 * MB, finished_hours=30),
                     Item("Just now", 1 * MB, finished_hours=0.2)])
    (w,) = v.warnings
    assert w.startswith("Finished in the last 24 hours: Fresh (3 h ago), Just now (less than an hour ago).")
    assert "Older" not in w and "may not have processed them yet" in w
    assert v.cancel_default  # the safe button has focus
    # the same meeting at exactly 24 h is no longer recent; audio-only does not warn (the reader does not use audio)
    assert not delete_view([Item("x", 1, finished_hours=24.0)]).warnings
    assert not delete_view([Item("Fresh", 1, finished_hours=1.0)], audio_only=True).warnings
    # permanent and recent both apply
    assert delete_view([Item("Fresh", 1, finished_hours=1.0)], permanent=True).warnings


def test_a_meeting_without_a_final_transcript_and_a_failed_audio_are_called_out():
    v = delete_view([Item("Half", 1 * MB, finished_hours=None, has_final=False)])
    assert v.warnings == ("No final transcript yet: Half.",)
    a = delete_view([Item("Broke", 1 * MB, final_failed=True), old("Fine")], audio_only=True)
    assert a.warnings == ("The final pass failed for Broke. Without the audio it cannot be retried.",)


def test_long_selections_are_shortened_and_unknown_sizes_left_out():
    v = delete_view([Item(f"m{i}", MB, finished_hours=99) for i in range(6)])
    assert "(m0, m1, m2 and 3 more)" in v.heading and "6.0 MB" in v.heading
    unknown = delete_view([Item("a", None, finished_hours=99), old("b")])
    assert unknown.heading == "Move 2 meetings (a, b) to the Recycle Bin?"


def test_format_size():
    f = confirm.format_size
    assert [f(0), f(500), f(2048), f(5 * MB), f(33 * MB), f(1536 * MB), f(None)] == [
        "0 bytes", "500 bytes", "2 KB", "5.0 MB", "33 MB", "1.5 GB", ""]


# -- rows and buttons ------------------------------------------------------------------------------------------
def test_rows_show_the_columns_and_follow_the_live_state(tmp_path):
    f, done = one(tmp_path / "a", title="Weekly sync")
    f2, failed = one(tmp_path / "b", title="Standup", status=mt.STATUS_FAILED, final=None)
    f2.update_meta(error="no model", duration_s=602)
    failed = library.scan(tmp_path / "b")[0]
    with_audio(f2, 2 * MB)
    cache = library.ScanCache()
    infos = library.scan(tmp_path / "b", cache)
    cache.wait_for_sizes()
    failed = library.scan(tmp_path / "b", cache)[0]
    (row,) = ml.meeting_rows([failed])
    assert row.values == ("01-15 09:30", "Standup", "10:02", "Final pass failed", "–", "0", "2.0 MB") and row.kind == "failed"
    (busy,) = ml.meeting_rows([failed], library.LiveState(queued=frozenset({failed.id}), finalizing=failed.id, progress=0.4))
    assert busy.values[3] == "Processing 40%"
    assert ml.meeting_rows([failed], status_filter="Complete") == [] and ml.meeting_rows([failed], search="stand")
    assert ml.summary_text([failed]).startswith("1 meeting · 2.0 MB")
    assert ml.summary_text([]) == "No meetings yet."
    assert ml.summary_text([failed, done], shown=1).startswith("1 of 2 meetings")


def test_buttons_follow_the_selection(tmp_path):
    _, done = one(tmp_path / "a", title="Done")
    f, failed = one(tmp_path / "b", title="Failed", status=mt.STATUS_FAILED, final=None)
    with_audio(f)
    failed = library.scan(tmp_path / "b")[0]
    none = ml.actions_for([])
    assert not any([none.open, none.export, none.retry, none.delete_meeting, none.delete_audio])
    a = ml.actions_for([done])
    assert (a.open, a.export, a.explorer, a.retry, a.delete_meeting, a.delete_audio) == (True, True, True, False, True, False)
    a = ml.actions_for([failed])
    assert a.retry and a.delete_audio and a.delete_meeting
    a = ml.actions_for([failed, done])
    assert not a.open and not a.export and a.retry and a.delete_meeting  # open/export are for one meeting
    live = library.LiveState(recording=done.id)
    a = ml.actions_for([done, failed], live)
    assert not a.delete_meeting and not a.delete_audio and "recording or being processed" in a.why
    assert ml.actions_for([failed], library.LiveState(waiting=frozenset({failed.id}))).retry is False


def test_detail_line_shows_the_error_or_the_queued_retry(tmp_path):
    f, failed = one(tmp_path / "b", status=mt.STATUS_FAILED, final=None)
    f.update_meta(error="model missing")
    failed = library.scan(tmp_path / "b")[0]
    text, level = ml.detail_text([failed])
    assert level == "error" and "model missing" in text
    text, level = ml.detail_text([failed], library.LiveState(waiting=frozenset({failed.id})))
    assert level == "info" and "Retry queued until recording stops" in text
    assert ml.detail_text([failed, failed]) == ("", "")


def test_confirm_items_use_audio_sizes_for_audio_and_skip_meetings_without_audio(tmp_path):
    f, with_a = one(tmp_path / "a", title="HasAudio")
    with_audio(f, 2000)
    _, without = one(tmp_path / "b", title="NoAudio")
    cache = library.ScanCache()
    infos = library.scan(tmp_path / "a", cache)
    cache.wait_for_sizes()
    has = library.scan(tmp_path / "a", cache)[0]
    items = ml.confirm_items([has, without], audio_only=True)
    assert [(i.title, i.bytes) for i in items] == [("HasAudio", 2000)]
    both = ml.confirm_items([has, without], audio_only=False)
    assert [i.title for i in both] == ["HasAudio", "NoAudio"] and both[0].bytes == has.total_bytes
    assert both[0].has_final and both[0].finished_hours is not None


def test_messages_after_actions():
    ok = library.DeleteResult(done=["a", "b"], how="recycled")
    assert ml.delete_message(ok, audio_only=False) == ("Moved 2 meetings to the Recycle Bin.", "info")
    assert ml.delete_message(library.DeleteResult(done=["a"], how="deleted"), audio_only=False)[0] == \
        "Permanently deleted 1 meeting."
    assert ml.delete_message(library.DeleteResult(done=["a"], how="recycled"), audio_only=True)[0] == \
        "Moved the audio of 1 meeting to the Recycle Bin."
    bad = library.DeleteResult(done=["a"], refused=["b"], failed=[("c", "in use")], how="recycled")
    text, level = ml.delete_message(bad, audio_only=False)
    assert level == "error" and "1 not deleted (recording or being processed)" in text and "in use" in text
    assert ml.retry_message(["started", "queued", "refused"])[1] == "error"
    assert ml.retry_message(["queued"]) == ("Retry queued until recording stops (1).", "info")


def test_load_model_reads_the_whole_final_transcript(tmp_path):
    many = [("system", i * 1000, i * 1000 + 500, "SPEAKER_01", f"line {i}.") for i in range(450)]
    folder = make_meeting(tmp_path, many, final=many)
    model = ml.load_model(folder)
    assert model.final and model.line_count == 450
