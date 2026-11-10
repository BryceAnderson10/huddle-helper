"""marks.jsonl: the reducer, MarkLog (ids, the 2 s rule, edits), and the controller's bookmark API."""

import threading

from huddle_helper import app, marks, meeting as mt
from huddle_helper.writer import read_events

from .helpers import make_meeting


def mark(mid, at_ms, kind="bookmark", **extra):
    return {"type": "mark", "id": mid, "kind": kind, "at_ms": at_ms, **extra}


# -- reducer ---------------------------------------------------------------------
def test_reduce_applies_label_kind_and_delete_in_order():
    state = marks.reduce([
        mark(1, 1000, "decision", via="hotkey", at="2026-01-15T09:30:01+00:00"),
        mark(2, 2000, "bookmark", via="line", seq=7),
        marks.label_event(1, "Ship Friday"),
        marks.kind_event(2, "action"),
        marks.label_event(2, "Dana to chase legal"),
        marks.label_event(2, ""),  # an empty label clears it
        mark(3, 3000),
        marks.delete_event(3),
    ])
    assert sorted(state) == [1, 2]
    assert (state[1].kind, state[1].label, state[1].at_ms, state[1].via) == ("decision", "Ship Friday", 1000, "hotkey")
    assert (state[2].kind, state[2].label, state[2].seq) == ("action", "", 7)


def test_unknown_kind_reads_as_bookmark_unless_the_meeting_defines_it():
    state = marks.reduce([mark(1, 0, "risk"), mark(2, 0, "decision"), mark(3, 0, "")])
    assert [state[i].read_kind() for i in (1, 2, 3)] == ["bookmark", "decision", "bookmark"]
    assert state[1].kind == "risk"  # the raw kind is kept
    assert state[1].read_kind({"risk": "Risk"}) == "risk"


def test_reduce_ignores_events_for_missing_ids_and_garbage():
    state = marks.reduce([marks.label_event(9, "x"), marks.kind_event(9, "action"), marks.delete_event(9),
                          {"type": "mark", "id": "x", "at_ms": 1}, {"type": "mark", "id": 4},
                          {"type": "other", "id": 1}, mark(1, 5)])
    assert list(state) == [1]
    assert marks.reduce([mark(1, 5)], {2: marks.Mark(2, "action", "", 9)}).keys() == {1, 2}  # continues a state


def test_the_transcript_model_uses_the_same_reducer():
    from huddle_helper import transcript_model

    assert transcript_model.reduce_marks is marks.reduce
    assert transcript_model.KIND_ORDER is marks.KIND_ORDER


def test_custom_kinds_and_summarize(tmp_path):
    folder = make_meeting(tmp_path, [], marks=[mark(1, 0, "risk"), mark(2, 0), mark(3, 0, "Bad Kind!"), mark(4, 0)])
    mt.append_jsonl(folder.marks_path, marks.delete_event(4))
    assert marks.summarize(folder) == {"marks": 3, "mark_kinds": {"risk": "Risk"}}
    empty = make_meeting(tmp_path / "e", [])
    assert marks.summarize(empty) == {"marks": 0, "mark_kinds": {}}


# -- MarkLog -------------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def new_log(tmp_path):
    folder = make_meeting(tmp_path, [])
    clock = Clock()
    return folder, marks.MarkLog(folder, clock), clock


def test_first_bookmark_creates_the_file_with_all_fields(tmp_path):
    folder, log, _ = new_log(tmp_path)
    assert not folder.marks_path.exists()
    res = log.add("decision", 381_200, "hotkey")
    assert res.as_dict() == {"action": "added", "id": 1, "kind": "decision", "at_ms": 381_200}
    (event,) = read_events(folder.marks_path)
    assert event["type"] == "mark" and event["id"] == 1 and event["kind"] == "decision"
    assert event["at_ms"] == 381_200 and event["via"] == "hotkey" and event["at"][:2] == "20" and "seq" not in event
    assert log.count == 1


def test_same_kind_within_two_seconds_is_ignored(tmp_path):
    folder, log, clock = new_log(tmp_path)
    log.add("bookmark", 1000)
    clock.t += 1.9
    assert log.add("bookmark", 2900).action == "ignored"
    assert len(read_events(folder.marks_path)) == 1 and log.count == 1
    clock.t += 0.2  # 2.1 s after the first press: a new bookmark
    res = log.add("bookmark", 5000)
    assert res.action == "added" and res.id == 2 and log.count == 2


def test_a_different_kind_within_two_seconds_retypes_the_previous_bookmark(tmp_path):
    folder, log, clock = new_log(tmp_path)
    log.add("bookmark", 1000)
    clock.t += 1.0
    res = log.add("decision", 2000)
    assert res.action == "retyped" and res.id == 1 and res.at_ms == 1000
    assert read_events(folder.marks_path)[1] == marks.kind_event(1, "decision")
    state = log.state()
    assert list(state) == [1] and state[1].kind == "decision" and log.count == 1
    clock.t += 1.0
    assert log.add("decision", 3000).action == "ignored"  # now it is a decision: the same type again


def test_the_two_second_rule_forgets_a_deleted_bookmark_and_skips_line_bookmarks(tmp_path):
    folder, log, clock = new_log(tmp_path)
    log.add("bookmark", 1000)
    log.edit(1, delete=True)
    clock.t += 0.5
    assert log.add("bookmark", 1500).action == "added"  # the one to compare with is gone
    # line bookmarks are deliberate: never ignored, never retyping a press
    assert log.add("bookmark", 500, "line", seq=3).action == "added"
    assert log.add("bookmark", 500, "line", seq=3).action == "added"
    assert log.add("question", 700, "line").action == "added"
    assert [m.via for m in log.state().values()] == ["hotkey", "line", "line", "line"]
    assert read_events(folder.marks_path)[-1]["via"] == "line"


def test_ids_come_from_the_file_and_are_never_reused(tmp_path):
    folder, log, clock = new_log(tmp_path)
    clock.t = 0
    log.add("bookmark", 1)
    clock.t = 10
    log.add("action", 2)
    log.edit(2, delete=True)
    clock.t = 20
    other = marks.MarkLog(folder, clock)  # a second writer for the same folder (a past-meeting edit)
    assert other.count == 1
    assert other.add("question", 3).id == 3 and log.add("bookmark", 4).id == 4


def test_edit_writes_only_what_changed(tmp_path):
    folder, log, clock = new_log(tmp_path)
    log.add("bookmark", 1000)
    n = len(read_events(folder.marks_path))
    assert log.edit(1, label="", kind="bookmark") is True
    assert len(read_events(folder.marks_path)) == n
    assert log.edit(1, label="  Ship it  ", kind="decision") is True
    tail = read_events(folder.marks_path)[n:]
    assert tail == [marks.label_event(1, "Ship it"), marks.kind_event(1, "decision")]
    assert log.edit(1, kind="not a kind!") is True and len(read_events(folder.marks_path)) == n + 2
    assert log.edit(99, label="x") is False
    assert log.edit(1, delete=True) and log.count == 0 and log.state() == {}


def test_concurrent_presses_get_distinct_ids(tmp_path):
    folder = make_meeting(tmp_path, [])
    logs = [marks.MarkLog(folder, double_press_s=0) for _ in range(4)]
    threads = [threading.Thread(target=lambda lg=lg: [lg.add("bookmark", i, "line") for i in range(25)]) for lg in logs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [e["id"] for e in read_events(folder.marks_path)]
    assert sorted(ids) == list(range(1, 101))


# -- the controller API (no recording: past meetings) -----------------------------------------------
def test_controller_bookmarks_a_line_of_a_past_meeting_and_edits_it(tmp_path):
    folder = make_meeting(tmp_path, [("mic", 5000, 6000, "You", "hello.")])
    ctl = app.AppController()
    res = ctl.bookmark("decision", "line", folder=folder, at_ms=5000, seq=1)
    assert res["action"] == "added" and res["id"] == 1
    (event,) = read_events(folder.marks_path)
    assert event["via"] == "line" and event["seq"] == 1 and event["at_ms"] == 5000
    assert event["at"] == "2026-01-15T09:30:05+00:00"  # the line's wall-clock time, not "now"
    assert folder.read_meta()["marks"] == 1
    assert ctl.edit_mark(folder, 1, label="Ship it", kind="action") is True
    assert ctl.edit_mark(folder, 7, label="x") is False
    assert [(m.kind, m.label) for m in marks.MarkLog(folder).state().values()] == [("action", "Ship it")]
    assert ctl.edit_mark(folder, 1, delete=True) and folder.read_meta()["marks"] == 0
    # nothing recording: a press has nowhere to go
    assert ctl.bookmark("bookmark")["action"] == "none" and ctl.bookmark("bookmark", folder=folder)["action"] == "none"
    assert ctl.hotkey_pressed("mute")["action"] == "none" and ctl.now_ms() is None and ctl.mic_muted is False
