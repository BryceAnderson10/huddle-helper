"""The read-only context command (#42): a bounded window of a meeting as JSON for a downstream app.

Tested at two seams: ``context.answer(meetings_dir, ...)`` (the dict the command prints) and
``cli.main(["context", ...])`` (JSON on stdout, exit code). All data is made up."""

import json
from datetime import timedelta

from huddle_helper import cli, context, meeting as mt
from huddle_helper.segments import Segment
from huddle_helper.writer import dumps, meeting_start_event, write_transcript

from .helpers import STARTED_AT, make_meeting

MIN = 60_000


def write_final(folder, lines, version):
    """Replace the final transcript (a re-run of the final pass) with these lines and version."""
    segs = [Segment(src, a, b, text, 0.8, spk) for src, a, b, spk, text in lines]
    write_transcript(folder.final_path, folder.meeting_id, mt.iso(STARTED_AT), mt.iso(STARTED_AT), segs,
                     transcript_version=version)
    folder.update_meta(transcript_version=version)
    folder.mark_done()


def done_meeting(tmp_path, live, final, version="v1", **kw):
    folder = make_meeting(tmp_path, live, final=final, **kw)
    write_final(folder, final, version)
    folder.update_meta(ended_at=mt.iso(STARTED_AT + timedelta(minutes=10)))
    return folder


def texts(ans):
    return [line["text"] for line in ans["lines"]]


# -- a finished meeting ------------------------------------------------------------------------------

def test_latest_finished_meeting_gives_the_last_three_minutes_of_the_final_transcript(tmp_path):
    live = [("system", 1 * MIN, 1 * MIN + 2000, "SPEAKER_01", "Live early."),
            ("system", 8 * MIN, 8 * MIN + 2000, "SPEAKER_01", "Live late.")]
    final = [("system", 1 * MIN, 1 * MIN + 2000, "SPEAKER_02", "Final early."),
             ("mic", 8 * MIN, 8 * MIN + 1500, "You", "Final late mic."),
             ("system", 9 * MIN, 9 * MIN + 2000, "SPEAKER_02", "Final later.")]
    folder = done_meeting(tmp_path, live, final, version="abc123")

    ans = context.answer(tmp_path)

    assert ans["ok"] is True
    m = ans["meeting"]
    assert (m["id"], m["title"], m["state"], m["active"], m["selected_as"]) == (
        folder.meeting_id, "Fixture", "complete", False, "latest")
    assert ans["transcript"]["kind"] == "final"
    assert ans["transcript"]["version"] == "abc123"
    # meeting ended at 10:00, so the window is 07:00-10:00
    assert (ans["window"]["start_ms"], ans["window"]["end_ms"]) == (7 * MIN, 10 * MIN)
    assert texts(ans) == ["Final late mic.", "Final later."]
    first = ans["lines"][0]
    assert first == {"stream": "mic", "start_ms": 8 * MIN, "end_ms": 8 * MIN + 1500, "text": "Final late mic.",
                     "confidence": 0.8, "speaker_label": "You", "speaker_name": None, "name_from": None}
    assert "generated_at" in ans


# -- choosing the meeting -------------------------------------------------------------------------------

def recording_meeting(tmp_path, title, started_ago_s, lines=(), beat_age_s=1.0, partial=None):
    """A meeting at status ``recording`` that started ``started_ago_s`` ago, with these live lines and a
    heartbeat ``beat_age_s`` old (None: no heartbeat file). ``partial``: a half-written last line."""
    now = mt.now_local()
    folder = mt.MeetingFolder.create(tmp_path, title, now - timedelta(seconds=started_ago_s), {})
    with open(folder.live_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps(meeting_start_event(folder.meeting_id, folder.read_meta()["started_at"])) + "\n")
        for i, seg in enumerate(Segment(src, a, b, text, 0.9, spk) for src, a, b, spk, text in lines):
            fh.write(dumps(seg.to_event(i + 1)) + "\n")
        if partial:
            fh.write(partial)
    if beat_age_s is None:
        folder.heartbeat_path.unlink()
    else:
        folder.beat(now - timedelta(seconds=beat_age_s))
    return folder


def test_active_meeting_is_returned_live_and_labelled_active(tmp_path):
    done_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "Old.")], [("system", 0, 1000, "SPEAKER_01", "Old.")])
    folder = recording_meeting(tmp_path, "Made Up Standup", 120,
                               [("mic", 10_000, 12_000, "You", "Hello there."),
                                ("system", 100_000, 103_000, "SPEAKER_01", "Morning.")])

    for which in ("active", "latest"):
        ans = context.answer(tmp_path, which)
        m = ans["meeting"]
        assert (m["id"], m["state"], m["active"], m["selected_as"]) == (folder.meeting_id, "recording", True, "active")
        assert m["heartbeat"]["state"] == "active"
        assert ans["transcript"]["kind"] == "live" and ans["transcript"]["version"] is None
        assert ans["transcript"]["reaches_ms"] == 103_000
        # the last 3 minutes up to now (about 120 s into the meeting): everything so far
        assert texts(ans) == ["Hello there.", "Morning."]
        assert "still_recording" in ans["window"]["incomplete_reasons"]


def test_a_stopped_or_dead_meeting_is_never_active_and_the_answer_names_the_one_that_just_ended(tmp_path):
    done_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "Old.")], [("system", 0, 1000, "SPEAKER_01", "Old.")])
    dead = recording_meeting(tmp_path, "Made Up Crash", 30, beat_age_s=60)

    ans = context.answer(tmp_path, "active")

    assert ans["ok"] is True and ans["meeting"] is None and ans["no_active_meeting"] is True
    assert ans["just_ended"]["id"] == dead.meeting_id
    assert ans["just_ended"]["state"] == "recorder_died" and ans["just_ended"]["active"] is False

    latest = context.answer(tmp_path)  # latest: the dead one, labelled as latest, not active
    assert latest["meeting"]["id"] == dead.meeting_id
    assert (latest["meeting"]["selected_as"], latest["meeting"]["active"]) == ("latest", False)


def test_a_meeting_that_started_seconds_ago_is_the_latest_even_if_empty(tmp_path):
    done_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "Old.")], [("system", 0, 1000, "SPEAKER_01", "Old.")])
    new = recording_meeting(tmp_path, "Made Up Kickoff", 3)

    ans = context.answer(tmp_path)

    assert ans["meeting"]["id"] == new.meeting_id and ans["meeting"]["active"] is True
    assert ans["lines"] == [] and ans["transcript"]["lines"] == 0


def test_a_named_meeting_by_id_and_an_unknown_id(tmp_path):
    old = done_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_01", "Old.")],
                       [("system", 0, 1000, "SPEAKER_01", "Old final.")])
    recording_meeting(tmp_path, "Made Up Standup", 60)

    ans = context.answer(tmp_path, old.meeting_id, whole=True)
    assert ans["meeting"]["id"] == old.meeting_id and ans["meeting"]["selected_as"] == "id"
    assert texts(ans) == ["Old final."]

    bad = context.answer(tmp_path, "2026-01-01_0000_nothing")
    assert bad["ok"] is False and bad["error"]["code"] == "not_found"
    assert context.answer(tmp_path / "empty")["error"]["code"] == "no_meetings"


def test_latest_is_by_start_time_not_by_folder_name(tmp_path):
    # Same minute: the folder names sort "aa" before "zz", but "aa" started 40 s later.
    make_meeting(tmp_path, [], title="Zz Review", started_at=STARTED_AT)
    later = make_meeting(tmp_path, [], title="Aa Kickoff", started_at=STARTED_AT + timedelta(seconds=40))

    assert context.answer(tmp_path)["meeting"]["id"] == later.meeting_id


# -- choosing the transcript ------------------------------------------------------------------------------

def test_live_on_request_and_a_half_written_last_line_is_left_out(tmp_path):
    done_meeting(tmp_path, [("system", 0, 2000, "SPEAKER_01", "Live words.")],
                          [("system", 0, 2000, "SPEAKER_03", "Final words.")], version="v9")
    assert texts(context.answer(tmp_path, transcript="live", whole=True)) == ["Live words."]
    live = context.answer(tmp_path, transcript="live", whole=True)["transcript"]
    assert (live["kind"], live["version"], live["reaches_ms"]) == ("live", None, 2000)

    growing = recording_meeting(tmp_path, "Made Up Sync", 60, [("mic", 1000, 2000, "You", "Complete.")],
                                partial='{"type":"segment","seq":2,"start_ms":3000,"end_ms":40')
    ans = context.answer(tmp_path)
    assert ans["meeting"]["id"] == growing.meeting_id
    assert texts(ans) == ["Complete."]
    with open(growing.live_path, "a", encoding="utf-8", newline="\n") as fh:  # the writer finishes the line
        fh.write('00,"source":"mic","speaker":"You","text":"Finished.","confidence":0.9}\n')
    assert texts(context.answer(tmp_path)) == ["Complete.", "Finished."]


def test_final_on_request_before_it_exists_is_an_error(tmp_path):
    recording_meeting(tmp_path, "Made Up Sync", 60)
    ans = context.answer(tmp_path, transcript="final")
    assert ans["ok"] is False and ans["error"]["code"] == "final_not_ready"


def test_a_new_final_transcript_shows_as_a_new_version(tmp_path):
    folder = done_meeting(tmp_path, [("system", 0, 2000, "SPEAKER_01", "Live.")],
                          [("system", 0, 2000, "SPEAKER_01", "First pass.")], version="v1")
    before = context.answer(tmp_path, whole=True)
    write_final(folder, [("system", 0, 2000, "SPEAKER_01", "Second pass.")], "v2")
    after = context.answer(tmp_path, whole=True)

    assert (before["transcript"]["version"], texts(before)) == ("v1", ["First pass."])
    assert (after["transcript"]["version"], texts(after)) == ("v2", ["Second pass."])


# -- choosing the window -------------------------------------------------------------------------------------

def long_meeting(tmp_path, **kw):
    """A done meeting with one final line a minute from 0:00 to 59:00, ended at 60:00."""
    final = [("system", i * MIN, i * MIN + 2000, "SPEAKER_01", f"Minute {i}.") for i in range(60)]
    folder = done_meeting(tmp_path, [], final, **kw)
    folder.update_meta(ended_at=mt.iso(STARTED_AT + timedelta(minutes=60)))
    return folder


def test_around_a_bookmark_is_three_minutes_before_and_thirty_seconds_after_by_its_time(tmp_path):
    # The bookmark was made on live line seq 2, which in the final file is unrelated text at another time.
    long_meeting(tmp_path, marks=[
        {"type": "mark", "id": 1, "kind": "decision", "at_ms": 30 * MIN, "at": "", "via": "line", "seq": 2},
        {"type": "mark_label", "id": 1, "label": "Made-up label", "changed_at": "2026-01-15T10:05:00.000+00:00"}])

    ans = context.answer(tmp_path, bookmark=1)

    w = ans["window"]
    assert (w["mode"], w["bookmark_id"], w["start_ms"], w["end_ms"]) == ("bookmark", 1, 27 * MIN, 30 * MIN + 30_000)
    assert texts(ans) == ["Minute 27.", "Minute 28.", "Minute 29.", "Minute 30."]
    assert ans["bookmarks"] == [{"id": 1, "type": "decision", "type_name": "Decision", "label": "Made-up label",
                                 "at_ms": 30 * MIN}]
    assert ans["bookmarks_changed_at"] == "2026-01-15T10:05:00.000+00:00"
    assert context.answer(tmp_path, bookmark=7)["error"]["code"] == "bookmark_not_found"


def test_time_bound_windows_are_capped_at_fifteen_minutes_and_whole_is_not(tmp_path):
    long_meeting(tmp_path)

    last = context.answer(tmp_path, last_s=40 * 60)
    assert (last["window"]["start_ms"], last["window"]["end_ms"], last["window"]["capped"]) == (45 * MIN, 60 * MIN, True)
    assert len(last["lines"]) == 15

    rng = context.answer(tmp_path, from_s=10 * 60, to_s=50 * 60)
    assert (rng["window"]["start_ms"], rng["window"]["end_ms"], rng["window"]["capped"]) == (10 * MIN, 25 * MIN, True)
    assert texts(rng)[0] == "Minute 10." and len(rng["lines"]) == 15

    small = context.answer(tmp_path, from_s=60, to_s=150)
    assert (small["window"]["capped"], texts(small)) == (False, ["Minute 1.", "Minute 2."])

    whole = context.answer(tmp_path, whole=True)
    assert (whole["window"]["mode"], whole["window"]["capped"], len(whole["lines"])) == ("whole", False, 60)


def test_conflicting_window_options_are_refused(tmp_path):
    long_meeting(tmp_path)
    for kw in ({"last_s": 60, "whole": True}, {"from_s": 60}, {"from_s": 90, "to_s": 60}, {"last_s": 0}):
        ans = context.answer(tmp_path, **kw)
        assert ans["ok"] is False and ans["error"]["code"] == "bad_request", kw


# -- names, gaps, attendees ------------------------------------------------------------------------------------

def test_live_names_come_from_the_users_tags(tmp_path):
    recording_meeting(tmp_path, "Made Up Sync", 60, [("system", 1000, 2000, "SPEAKER_01", "One."),
                                                     ("system", 3000, 4000, "SPEAKER_02", "Two.")])
    folder = next(mt.iter_meetings(tmp_path))
    mt.append_jsonl(folder.tags_path, {"type": "tag", "at": "", "scope": "speaker", "label": "SPEAKER_01",
                                       "name": "Pat Example"})

    lines = context.answer(tmp_path)["lines"]

    assert [(x["speaker_label"], x["speaker_name"], x["name_from"]) for x in lines] == [
        ("SPEAKER_01", "Pat Example", "speaker_tags.jsonl"), ("SPEAKER_02", None, None)]


def test_final_names_come_from_speakers_json_with_their_conflicts(tmp_path):
    folder = done_meeting(tmp_path, [], [("system", 9 * MIN, 9 * MIN + 2000, "SPEAKER_02", "Hi."),
                                         ("system", 9 * MIN + 3000, 9 * MIN + 4000, "SPEAKER_03", "Yo.")])
    conflict = {"speaker": "SPEAKER_02", "names": {"Robin Sample": 4000, "Sam Placeholder": 1500}}
    folder.merge_speakers({"SPEAKER_02": "Robin Sample", "SPEAKER_04": "Not In Window"}, conflicts=[
        conflict, {"speaker": "SPEAKER_04", "names": {"Not In Window": 1000, "Other": 900}}])

    ans = context.answer(tmp_path)

    assert [(x["speaker_name"], x["name_from"]) for x in ans["lines"]] == [("Robin Sample", "speakers.json"), (None, None)]
    assert ans["speakers"]["conflicts"] == [conflict]
    assert ans["speakers"]["names_changed_at"] == mt.read_json(folder.speakers_path)["names_changed_at"]


def test_gaps_in_the_window_say_whether_they_were_deliberate(tmp_path):
    gaps = [{"source": "mic", "start_ms": 8 * MIN, "end_ms": 8 * MIN + 30_000, "reason": "muted"},
            {"source": "system", "start_ms": 9 * MIN, "end_ms": 9 * MIN + 5000, "reason": "device_change"},
            {"source": "system", "start_ms": 1 * MIN, "end_ms": 2 * MIN, "reason": "device_change"}]
    done_meeting(tmp_path, [], [("system", 9 * MIN + 6000, 9 * MIN + 8000, "SPEAKER_01", "Back.")], gaps=gaps)

    ans = context.answer(tmp_path)

    assert ans["gaps"] == [
        {"stream": "mic", "start_ms": 8 * MIN, "end_ms": 8 * MIN + 30_000, "reason": "muted", "deliberate": True},
        {"stream": "system", "start_ms": 9 * MIN, "end_ms": 9 * MIN + 5000, "reason": "device_change",
         "deliberate": False}]
    assert ans["window"]["incomplete"] is True and ans["window"]["incomplete_reasons"] == ["lost_audio"]

    only_muted = context.answer(tmp_path, from_s=7 * 60, to_s=8 * 60 + 40)
    assert only_muted["window"]["incomplete"] is False


def test_a_mute_still_in_progress_is_an_open_deliberate_gap(tmp_path):
    folder = recording_meeting(tmp_path, "Made Up Sync", 60)
    folder.update_meta(mic_muted_since_ms=20_000)

    gaps = context.answer(tmp_path)["gaps"]

    assert gaps == [{"stream": "mic", "start_ms": 20_000, "end_ms": None, "reason": "muted", "deliberate": True,
                     "open": True}]


def test_attendees_are_labelled_as_attendees_only(tmp_path):
    folder = done_meeting(tmp_path, [], [])
    folder.update_meta(attendees=["Pat Example", "Robin Sample"])

    att = context.answer(tmp_path)["attendees"]

    assert att["names"] == ["Pat Example", "Robin Sample"]
    assert "not who spoke" in att["meaning"].lower() and "not a record of who attended" in att["meaning"].lower()


# -- files that cannot be read ------------------------------------------------------------------------------------

def test_an_unreadable_file_gives_an_error_result(tmp_path):
    folder = done_meeting(tmp_path, [], [])
    folder.final_path.unlink()
    folder.final_path.mkdir()  # exists, cannot be opened as a file
    ans = context.answer(tmp_path, transcript="final")
    assert ans["ok"] is False and ans["error"]["code"] == "unreadable"
    assert ans["error"]["file"].endswith("transcript.final.jsonl")

    folder.meta_path.write_text("{half", encoding="utf-8")
    bad_meta = context.answer(tmp_path, folder.meeting_id)
    assert bad_meta["ok"] is False and bad_meta["error"]["code"] == "unreadable"


# -- the command ------------------------------------------------------------------------------------------------

def test_the_command_prints_the_answer_as_json(tmp_path, capsys):
    long_meeting(tmp_path / "meetings")

    code = cli.main(["context", "--meetings-dir", str(tmp_path / "meetings"), "--from", "60", "--to", "150"])

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True
    assert [x["text"] for x in out["lines"]] == ["Minute 1.", "Minute 2."]

    code = cli.main(["context", "--meetings-dir", str(tmp_path / "meetings"), "--bookmark", "3"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error"]["code"] == "bookmark_not_found"


def test_the_command_rejects_two_window_options(tmp_path, capsys):
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["context", "--meetings-dir", str(tmp_path), "--whole", "--last", "60"])


def test_an_unreadable_newer_meeting_is_an_error_but_an_older_one_only_a_warning(tmp_path):
    old = make_meeting(tmp_path, [], title="Old", started_at=STARTED_AT)
    new = make_meeting(tmp_path, [], title="New", started_at=STARTED_AT + timedelta(hours=1))

    old.meta_path.write_text("{half", encoding="utf-8")
    ans = context.answer(tmp_path)
    assert ans["ok"] is True and ans["meeting"]["id"] == new.meeting_id
    assert ans["warnings"][0]["code"] == "unreadable"

    new.meta_path.write_text("{half", encoding="utf-8")
    ans = context.answer(tmp_path)
    assert ans["ok"] is False and ans["error"]["code"] == "unreadable"


def test_non_finite_times_are_refused(tmp_path):
    make_meeting(tmp_path, [])
    for kw in ({"last_s": float("nan")}, {"from_s": 0, "to_s": float("inf")}):
        assert context.answer(tmp_path, **kw)["error"]["code"] == "bad_request"
