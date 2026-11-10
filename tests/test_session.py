"""MeetingSession: the window's polling orchestration, without Tk."""

import json

from huddle_helper import meeting as mt
from huddle_helper.session import MeetingSession
from huddle_helper.transcript_model import GapRow, Insert, LineRow, Reset

from .helpers import make_meeting


def seg(seq, source, start, end, speaker, text="x."):
    return {"type": "segment", "seq": seq, "start_ms": start, "end_ms": end, "source": source,
            "speaker": speaker, "text": text, "confidence": 0.9}


def append_event(path, event):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event) + "\n")


def test_follows_a_live_file_as_it_grows(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "one.")], status=mt.STATUS_RECORDING)
    s = MeetingSession(folder)
    first = s.poll()
    assert [type(o) for o in first] == [Insert] and s.model.line_count == 1 and s.model.provisional
    assert s.poll() == []
    append_event(folder.live_path, seg(2, "system", 2000, 3000, "SPEAKER_01", "two."))
    ops = s.poll()
    assert len(ops) == 1 and isinstance(ops[0].row, LineRow) and ops[0].row.text == "two."


def test_poll_is_capped_and_says_more_is_waiting(tmp_path):
    lines = [("mic", i * 1000, i * 1000 + 500, "You", f"l{i}.") for i in range(10)]
    s = MeetingSession(make_meeting(tmp_path, lines))
    # the file also holds meeting_start and meeting_end events, which count towards the cap
    assert len(s.poll(max_events=4)) == 3 and s.has_more and s.model.line_count == 3
    assert len(s.poll(max_events=4)) == 4 and s.has_more
    s.poll(max_events=4)
    assert s.model.line_count == 10 and not s.has_more


def test_partial_last_line_waits_for_its_newline(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "one.")], status=mt.STATUS_RECORDING)
    s = MeetingSession(folder)
    s.poll()
    line = json.dumps(seg(2, "mic", 2000, 3000, "You", "half."))
    with open(folder.live_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line[:20])
    assert s.poll() == []
    with open(folder.live_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line[20:] + "\n")
    assert len(s.poll()) == 1


def test_opens_on_the_final_file_when_done_exists(tmp_path):
    folder = make_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_00", "live.")],
                          final=[("system", 0, 1000, "SPEAKER_07", "final.")])
    folder.write_speakers({"SPEAKER_07": "Dana"})
    s = MeetingSession(folder)
    s.poll()
    assert s.is_final and not s.model.provisional
    row = s.model.lines[0]
    assert (row.text, row.label, row.name) == ("final.", "SPEAKER_07", "Dana")


def test_swaps_to_final_when_done_appears_and_reports_changed_lines(tmp_path):
    live = [("system", 0, 2000, "SPEAKER_00", "a."), ("system", 3000, 5000, "SPEAKER_01", "b."),
            ("system", 6000, 8000, "SPEAKER_00", "c."), ("system", 9000, 11000, "SPEAKER_00", "d.")]
    folder = make_meeting(tmp_path, live, status=mt.STATUS_PROCESSING)
    s = MeetingSession(folder)
    s.poll()
    assert s.poll_slow().swapped is False
    # the final pass writes its file, then DONE
    from .helpers import _segments, _write_events

    final = [("system", 0, 2000, "SPEAKER_00", "a."), ("system", 3000, 5000, "SPEAKER_01", "b."),
             ("system", 6000, 8000, "SPEAKER_00", "c."), ("system", 9000, 11000, "SPEAKER_01", "d.")]
    _write_events(folder.final_path, folder.meeting_id, "2026-01-15T09:30:00+00:00", _segments(final))
    assert s.poll_slow().swapped is False  # no DONE yet
    folder.update_meta(status=mt.STATUS_COMPLETE)
    folder.mark_done()
    slow = s.poll_slow()
    assert slow.swapped and any(isinstance(o, Reset) for o in slow.ops)
    assert slow.changed == {("system", 9000)}
    assert s.is_final and s.model.line_count == 4
    assert s.poll() == [] and s.poll_slow().swapped is False  # nothing is applied twice


def test_swap_sees_live_lines_the_view_had_not_read_yet(tmp_path):
    lines = [("system", i * 1000, i * 1000 + 800, "SPEAKER_00", f"l{i}.") for i in range(6)]
    folder = make_meeting(tmp_path, lines, final=None, status=mt.STATUS_PROCESSING)
    s = MeetingSession(folder)
    s.poll(max_events=2)  # most of the live file is still unread
    from .helpers import _segments, _write_events

    final = [("system", i * 1000, i * 1000 + 800, "SPEAKER_00" if i < 5 else "SPEAKER_01", f"l{i}.")
             for i in range(6)]
    _write_events(folder.final_path, folder.meeting_id, "2026-01-15T09:30:00+00:00", _segments(final))
    folder.mark_done()
    slow = s.poll_slow()
    assert slow.swapped and s.model.line_count == 6 and slow.changed == {("system", 5000)}


def test_speakers_json_changes_rename_final_lines(tmp_path):
    folder = make_meeting(tmp_path, [("system", 0, 1000, "SPEAKER_00", "a.")],
                          final=[("system", 0, 1000, "SPEAKER_00", "a.")])
    s = MeetingSession(folder)
    s.poll()
    assert s.model.lines[0].name is None
    folder.write_speakers({"SPEAKER_00": "Eric"})
    slow = s.poll_slow()
    assert slow.ops and s.model.lines[0].name == "Eric"


def test_meta_gaps_become_rows_and_new_gaps_arrive_by_poll_slow(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "x.")], status=mt.STATUS_RECORDING,
                          gaps=[{"source": "mic", "start_ms": 1500, "end_ms": 2500, "reason": "muted"}])
    s = MeetingSession(folder)
    assert [type(r) for r in s.model.rows] == [GapRow]  # in the model from meta.json at once
    s.poll()
    folder.add_gap("system", 5000, 9000, "device changed")
    slow = s.poll_slow()
    assert any(isinstance(o, Insert) and isinstance(o.row, GapRow) for o in slow.ops)
    assert len(s.model.gaps) == 2
    assert s.poll_slow().ops == []  # meta unchanged: nothing re-read


def test_file_rewritten_shorter_resets_the_lines(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "one."), ("mic", 2000, 3000, "You", "two.")],
                          status=mt.STATUS_RECORDING)
    s = MeetingSession(folder)
    s.poll()
    folder.live_path.write_text(json.dumps(seg(1, "mic", 0, 1000, "You", "again.")) + "\n", encoding="utf-8")
    ops = s.poll()
    assert any(isinstance(o, Reset) for o in ops)
    assert [r.text for r in s.model.lines] == ["again."]


def test_missing_files_are_not_an_error(tmp_path):
    folder = mt.MeetingFolder.create(tmp_path, "empty", mt.now_local(), {})
    s = MeetingSession(folder)
    assert s.poll() == [] and s.poll_slow().ops == [] and s.model.rows == []
