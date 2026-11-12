"""The downstream-app scenarios (#43) that ``test_context.py`` did not already cover, against
``context.answer``. Same fixture builders as the command's own tests; all data is made up.

Rows of the #43 table already covered elsewhere (not repeated here):
    * half-written live line: ``test_context.test_live_on_request_and_a_half_written_last_line_is_left_out``
      (and ``test_tail`` / ``test_session`` for the reader underneath)
    * a gap in the window, ``muted`` as deliberate: ``test_context.test_gaps_in_the_window_say_whether_they_were_deliberate``
      and ``test_a_mute_still_in_progress_is_an_open_deliberate_gap`` (one edge case added below)
"""

from datetime import timedelta

from huddle_helper import context, finalize, meeting as mt
from huddle_helper.segments import Segment

from .helpers import STARTED_AT, make_meeting
from .test_context import MIN, done_meeting, recording_meeting, texts, write_final


# -- Recording, nobody speaking -----------------------------------------------------------------------------

def test_a_recording_with_nobody_speaking_is_healthy_and_active_not_an_error(tmp_path):
    # Ten minutes in, one line at minute 1 and silence since; the recorder is beating.
    folder = recording_meeting(tmp_path, "Made Up Focus Time", 10 * 60,
                               [("mic", 1 * MIN, 1 * MIN + 2000, "You", "Going quiet now.")])

    ans = context.answer(tmp_path, "active")

    assert ans["ok"] is True and "error" not in ans and ans["warnings"] == []
    m = ans["meeting"]
    assert (m["id"], m["state"], m["active"], m["heartbeat"]["state"]) == (folder.meeting_id, "recording", True, "active")
    assert ans["lines"] == [] and ans["gaps"] == []  # the last 3 minutes are silent
    assert ans["transcript"]["lines"] == 1
    # Silence is neither lost audio nor a dead recorder; only "still recording".
    assert ans["window"]["incomplete_reasons"] == ["still_recording"]


# -- Nothing recording ------------------------------------------------------------------------------------

def test_after_a_clean_stop_there_is_no_active_meeting_and_latest_is_the_one_that_ended(tmp_path):
    done_meeting(tmp_path, [], [("system", 0, 1000, "SPEAKER_01", "Older final.")])
    ended = make_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "Newer live.")],
                         final=[("system", 0, 1000, "SPEAKER_01", "Newer final.")],
                         title="Made Up Retro", started_at=STARTED_AT + timedelta(hours=2))

    ans = context.answer(tmp_path, "active")

    assert ans["ok"] is True and ans["meeting"] is None and ans["no_active_meeting"] is True
    just = ans["just_ended"]
    assert (just["id"], just["title"], just["state"], just["active"]) == (
        ended.meeting_id, "Made Up Retro", "complete", False)
    assert just["heartbeat"]["state"] == "not_recording"

    latest = context.answer(tmp_path, "latest", whole=True)
    m = latest["meeting"]
    assert (m["id"], m["selected_as"], m["active"], m["state"]) == (ended.meeting_id, "latest", False, "complete")
    assert texts(latest) == ["Newer final."]
    assert "still_recording" not in latest["window"]["incomplete_reasons"]


# -- Final pass renumbers or removes a bookmarked line's seq -------------------------------------------------

def test_a_bookmark_whose_line_seq_is_gone_from_the_final_still_resolves_by_time(tmp_path):
    # Live: forty short lines, the bookmark made on the last one (seq 40, at 20:00).
    live = [("system", i * 30_000, i * 30_000 + 2000, "SPEAKER_01", f"Live bit {i}.") for i in range(41)]
    # Final: the pass joined them into three lines, so there is no seq 40 at all, and seq 2 is elsewhere.
    final = [("system", 0, 9 * MIN, "SPEAKER_01", "Final opening."),
             ("system", 18 * MIN, 20 * MIN + 10_000, "SPEAKER_01", "Final around the bookmark."),
             ("system", 30 * MIN, 31 * MIN, "SPEAKER_01", "Final much later.")]
    folder = done_meeting(tmp_path, live, final, marks=[
        {"type": "mark", "id": 1, "kind": "action", "at_ms": 20 * MIN, "at": "", "via": "line", "seq": 40}])
    folder.update_meta(ended_at=mt.iso(STARTED_AT + timedelta(minutes=32)))

    ans = context.answer(tmp_path, bookmark=1)

    assert ans["ok"] is True and ans["transcript"]["kind"] == "final"
    w = ans["window"]
    assert (w["start_ms"], w["end_ms"]) == (17 * MIN, 20 * MIN + 30_000)
    assert texts(ans) == ["Final around the bookmark."]
    assert [b["id"] for b in ans["bookmarks"]] == [1] and ans["bookmarks"][0]["at_ms"] == 20 * MIN


# -- Live and final disagree on a quantity --------------------------------------------------------------------

def test_when_live_and_final_disagree_on_a_quantity_both_wordings_and_the_version_are_available(tmp_path):
    folder = done_meeting(tmp_path, [("system", 2 * MIN, 2 * MIN + 3000, "SPEAKER_01", "Order fifteen crates.")],
                          [("system", 2 * MIN, 2 * MIN + 3000, "SPEAKER_01", "Order fifty crates.")], version="v2")

    auto = context.answer(tmp_path, whole=True)
    final = context.answer(tmp_path, transcript="final", whole=True)
    live = context.answer(tmp_path, transcript="live", whole=True)

    assert (auto["transcript"]["kind"], auto["transcript"]["version"], texts(auto)) == (
        "final", "v2", ["Order fifty crates."])
    assert (final["transcript"]["kind"], final["transcript"]["version"], texts(final)) == (
        "final", "v2", ["Order fifty crates."])
    assert (live["transcript"]["kind"], live["transcript"]["version"], texts(live)) == (
        "live", None, ["Order fifteen crates."])
    # Same time on the meeting clock, so a reader can pair the two wordings.
    assert final["lines"][0]["start_ms"] == live["lines"][0]["start_ms"]
    assert folder.live_path.exists()  # the final pass leaves the live file in place


# -- Speaker labels change in the final pass -----------------------------------------------------------------

def test_names_and_conflicts_survive_the_final_pass_relabelling_the_speakers(tmp_path):
    # Live labels, each named by the user in the window (speaker_tags.jsonl), except SPEAKER_04.
    live = [("system", 0, 4000, "SPEAKER_01", "Pat speaks."),
            ("system", 5000, 9000, "SPEAKER_02", "Robin speaks."),
            ("system", 10_000, 13_000, "SPEAKER_03", "Sam speaks."),
            ("system", 14_000, 16_000, "SPEAKER_04", "Nobody named this one.")]
    tags = [{"type": "tag", "at": "", "scope": "speaker", "label": label, "name": name}
            for label, name in (("SPEAKER_01", "Pat Example"), ("SPEAKER_02", "Robin Sample"),
                                ("SPEAKER_03", "Sam Placeholder"))]
    # The final pass swaps the labels around, puts Robin and Sam into one speaker, and gives the unnamed
    # voice the label Sam had live.
    final = [("system", 0, 4000, "SPEAKER_02", "Pat speaks."),
             ("system", 5000, 9000, "SPEAKER_01", "Robin speaks."),
             ("system", 10_000, 13_000, "SPEAKER_01", "Sam speaks."),
             ("system", 14_000, 16_000, "SPEAKER_03", "Nobody named this one.")]
    folder = make_meeting(tmp_path, live, tags=tags)
    before = context.answer(tmp_path, whole=True)  # live, before the final pass

    write_final(folder, final, "v1")
    as_segments = lambda rows: [Segment(s, a, b, t, 0.9, spk) for s, a, b, spk, t in rows]  # noqa: E731
    assert finalize.write_names(folder, as_segments(live), as_segments(final))
    after = context.answer(tmp_path, whole=True)

    named = lambda ans: [(x["speaker_label"], x["speaker_name"], x["name_from"]) for x in ans["lines"]]  # noqa: E731
    assert named(before) == [("SPEAKER_01", "Pat Example", "speaker_tags.jsonl"),
                             ("SPEAKER_02", "Robin Sample", "speaker_tags.jsonl"),
                             ("SPEAKER_03", "Sam Placeholder", "speaker_tags.jsonl"),
                             ("SPEAKER_04", None, None)]
    assert after["transcript"]["kind"] == "final"
    # Names follow the voices, not the labels; the live tag on SPEAKER_03 is not carried over to the label.
    assert named(after) == [("SPEAKER_02", "Pat Example", "speakers.json"),
                            ("SPEAKER_01", "Robin Sample", "speakers.json"),
                            ("SPEAKER_01", "Robin Sample", "speakers.json"),
                            ("SPEAKER_03", None, None)]
    assert after["speakers"]["conflicts"] == [
        {"speaker": "SPEAKER_01", "names": {"Robin Sample": 4000, "Sam Placeholder": 3000}}]
    assert after["speakers"]["names_changed_at"] is not None


# -- Window overlaps a gap (edge case; the main row is in test_context) -----------------------------------------

def test_a_gap_that_began_before_the_window_and_runs_into_it_is_reported(tmp_path):
    gaps = [{"source": "system", "start_ms": 6 * MIN + 30_000, "end_ms": 7 * MIN + 20_000, "reason": "device_change"},
            {"source": "system", "start_ms": 6 * MIN, "end_ms": 6 * MIN + 59_000, "reason": "device_change"}]
    done_meeting(tmp_path, [], [("system", 9 * MIN, 9 * MIN + 2000, "SPEAKER_01", "Later.")], gaps=gaps)

    ans = context.answer(tmp_path)  # 07:00-10:00

    assert [(g["start_ms"], g["end_ms"], g["deliberate"]) for g in ans["gaps"]] == [
        (6 * MIN + 30_000, 7 * MIN + 20_000, False)]
    assert ans["window"]["incomplete_reasons"] == ["lost_audio"]


# -- An attendee never speaks / a speaker has no name -----------------------------------------------------------

def test_an_unnamed_speaker_stays_unnamed_even_with_one_attendee_left_over(tmp_path):
    # Two attendees, two voices; only one voice was named. The obvious guess must not be made.
    lines = [("system", 1000, 3000, "SPEAKER_01", "Named voice."),
             ("system", 4000, 6000, "SPEAKER_02", "Unnamed voice.")]
    folder = recording_meeting(tmp_path, "Made Up Sync", 60, lines)
    folder.update_meta(attendees=["Pat Example", "Robin Sample", "Sam Placeholder"])
    mt.append_jsonl(folder.tags_path, {"type": "tag", "at": "", "scope": "speaker", "label": "SPEAKER_01",
                                       "name": "Pat Example"})

    live = context.answer(tmp_path)

    assert [(x["speaker_label"], x["speaker_name"], x["name_from"]) for x in live["lines"]] == [
        ("SPEAKER_01", "Pat Example", "speaker_tags.jsonl"), ("SPEAKER_02", None, None)]
    assert live["attendees"]["names"] == ["Pat Example", "Robin Sample", "Sam Placeholder"]

    # Finished, with speakers.json naming only SPEAKER_01: the same holds on the final transcript.
    write_final(folder, lines, "v1")
    folder.update_meta(status=mt.STATUS_COMPLETE, ended_at=mt.iso(mt.now_local()))
    folder.merge_speakers({"SPEAKER_01": "Pat Example"})
    final = context.answer(tmp_path, whole=True)

    assert final["transcript"]["kind"] == "final"
    assert [(x["speaker_label"], x["speaker_name"], x["name_from"]) for x in final["lines"]] == [
        ("SPEAKER_01", "Pat Example", "speakers.json"), ("SPEAKER_02", None, None)]
    assert final["speakers"]["conflicts"] == []
    assert final["attendees"]["names"] == ["Pat Example", "Robin Sample", "Sam Placeholder"]
