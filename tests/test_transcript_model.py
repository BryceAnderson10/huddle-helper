from huddle_helper.transcript_model import (GapRow, Insert, LineRow, MarkRow, Remove, Reset, TranscriptModel, Update,
                                             changed_after_swap, fmt_elapsed, reduce_marks)

from .helpers import make_meeting


def seg(seq, source, start, end, speaker, text="x."):
    return {"type": "segment", "seq": seq, "start_ms": start, "end_ms": end, "source": source,
            "speaker": speaker, "text": text, "confidence": 0.9}


def mark(mid, at_ms, kind="bookmark", **extra):
    return {"type": "mark", "id": mid, "kind": kind, "at_ms": at_ms, **extra}


def kinds(model):
    return [type(r).__name__[0] + str(getattr(r, "seq", None) or getattr(r, "id", None) or "") for r in model.rows]


def test_rows_are_ordered_with_marks_and_gaps():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "mic", 1000, 3000, "You"), seg(2, "system", 5000, 8000, "SPEAKER_01"),
                        seg(3, "mic", 12000, 14000, "You")])
    m.apply_marks([mark(1, 9000)])
    m.set_meta({"gaps": [{"source": "mic", "start_ms": 8500, "end_ms": 11000, "reason": "muted"}]})
    assert [type(r) for r in m.rows] == [LineRow, LineRow, GapRow, MarkRow, LineRow]
    assert [r.at_ms for r in m.rows] == [1000, 5000, 8500, 9000, 12000]


def test_line_at_same_time_sorts_before_gap_before_mark():
    m = TranscriptModel()
    m.apply_marks([mark(1, 5000)])
    m.set_meta({"gaps": [{"source": "system", "start_ms": 5000, "end_ms": 6000, "reason": "device"}]})
    m.apply_transcript([seg(1, "mic", 5000, 5500, "You")])
    assert [type(r) for r in m.rows] == [LineRow, GapRow, MarkRow]


def test_late_line_lands_above_an_earlier_bookmark():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "mic", 1000, 2000, "You")])
    ops = m.apply_marks([mark(1, 10_000)])
    assert ops == [Insert(1, MarkRow(1, "bookmark", "", 10_000))]
    # the line being spoken at the press arrives afterwards, 1-2 s late
    ops = m.apply_transcript([seg(2, "system", 9500, 11_500, "SPEAKER_01", "late")])
    assert ops == [Insert(1, m.rows[1])] and isinstance(m.rows[1], LineRow) and m.rows[1].text == "late"
    assert isinstance(m.rows[2], MarkRow)


def test_duplicate_seq_is_ignored():
    m = TranscriptModel()
    events = [seg(1, "mic", 0, 1000, "You")]
    assert len(m.apply_transcript(events)) == 1
    assert m.apply_transcript(events) == []
    assert m.line_count == 1


def test_mark_label_kind_delete_become_update_and_remove_ops():
    m = TranscriptModel()
    m.apply_marks([mark(1, 1000), mark(2, 2000)])
    ops = m.apply_marks([{"type": "mark_label", "id": 1, "label": "Ship it"},
                         {"type": "mark_kind", "id": 2, "kind": "decision"}])
    assert ops == [Update(0, MarkRow(1, "bookmark", "Ship it", 1000)), Update(1, MarkRow(2, "decision", "", 2000))]
    assert m.apply_marks([{"type": "mark_delete", "id": 1}]) == [Remove(0)]
    assert [r.id for r in m.marks] == [2]


def test_unknown_mark_kind_reads_as_bookmark_unless_defined_in_meta():
    m = TranscriptModel()
    m.apply_marks([mark(1, 1000, "risk")])
    assert m.marks[0].kind == "bookmark"
    ops = m.set_meta({"mark_kinds": {"risk": "Risk"}})
    assert m.marks[0].kind == "risk" and ops == [Update(0, m.marks[0])]
    assert m.kind_name("risk") == "Risk"


def test_reduce_marks_ignores_events_for_unknown_ids():
    assert reduce_marks([{"type": "mark_label", "id": 4, "label": "a"}]) == {}


def test_set_meta_diffs_gaps():
    m = TranscriptModel()
    gap = {"source": "mic", "start_ms": 1000, "end_ms": 2000, "reason": "muted"}
    assert len(m.set_meta({"gaps": [gap]})) == 1
    assert m.set_meta({"gaps": [gap]}) == []
    assert m.set_meta({"gaps": []}) == [Remove(0)]


def test_live_names_line_tag_beats_speaker_tag_beats_label():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "system", 1000, 2000, "SPEAKER_01"), seg(2, "system", 3000, 4000, "SPEAKER_01"),
                        seg(3, "system", 5000, 6000, "SPEAKER_02")])
    assert [r.display for r in m.lines] == ["SPEAKER_01", "SPEAKER_01", "SPEAKER_02"]
    ops = m.apply_tags([{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Eric"}])
    assert [type(o) for o in ops] == [Update, Update] and [r.name for r in m.lines] == ["Eric", "Eric", None]
    m.apply_tags([{"type": "tag", "scope": "line", "seq": 2, "source": "system", "start_ms": 3000,
                   "end_ms": 4000, "name": "Dana"}])
    assert [r.display for r in m.lines] == ["Eric", "Dana", "SPEAKER_02"]
    # a line that arrives after the speaker tag is named at once
    m.apply_transcript([seg(4, "system", 7000, 8000, "SPEAKER_01")])
    assert m.lines[-1].name == "Eric"
    # later events win; untag falls back
    m.apply_tags([{"type": "untag", "scope": "line", "source": "system", "start_ms": 3000},
                  {"type": "untag", "scope": "speaker", "label": "SPEAKER_01"}])
    assert [r.name for r in m.lines] == [None, None, None, None]


def test_two_labels_with_one_name_are_one_person():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "system", 1000, 2000, "SPEAKER_01"), seg(2, "system", 3000, 4000, "SPEAKER_03")])
    m.apply_tags([{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Eric"},
                  {"type": "tag", "scope": "speaker", "label": "SPEAKER_03", "name": "Eric"}])
    people = m.speakers()
    assert len(people) == 1 and people[0].person_key == "Eric"
    assert people[0].labels == ("SPEAKER_01", "SPEAKER_03") and people[0].lines == 2


def test_final_names_come_from_speakers_json_and_tags_are_ignored():
    m = TranscriptModel()
    m.apply_tags([{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Live name"}])
    result = m.switch_to_final([seg(1, "system", 1000, 2000, "SPEAKER_01"), seg(2, "mic", 3000, 4000, "You")],
                               {"SPEAKER_01": "Dana"}, [{"speaker": "SPEAKER_01", "names": {"Dana": 5}}])
    assert result.ops == [Reset()] and m.final and not m.provisional
    assert [r.display for r in m.lines] == ["Dana", "You"]
    assert m.conflicts == [{"speaker": "SPEAKER_01", "names": {"Dana": 5}}]
    # tags arriving now change nothing; a rename in speakers.json updates the rows
    assert m.apply_tags([{"type": "tag", "scope": "speaker", "label": "You", "name": "Me"}]) == []
    ops = m.set_speakers({"SPEAKER_01": "Dana", "You": "Bryce"})
    assert [type(o) for o in ops] == [Update] and m.lines[1].name == "Bryce"


def test_speakers_json_is_remembered_but_unused_while_live():
    m = TranscriptModel()
    m.set_speakers({"SPEAKER_01": "Dana"})
    m.apply_transcript([seg(1, "system", 1000, 2000, "SPEAKER_01")])
    assert m.lines[0].name is None and m.provisional


def test_swap_marks_only_lines_whose_person_changed():
    live = [seg(1, "system", 0, 10_000, "SPEAKER_01"), seg(2, "system", 10_000, 20_000, "SPEAKER_02"),
            seg(3, "system", 20_000, 30_000, "SPEAKER_01"), seg(4, "system", 30_000, 40_000, "SPEAKER_01"),
            seg(5, "mic", 41_000, 45_000, "You")]
    # Final numbering is different (labels swapped), line 4 moved to the other person, line 2 is split.
    final = [seg(1, "system", 0, 10_000, "SPEAKER_02"), seg(2, "system", 10_000, 15_000, "SPEAKER_01"),
             seg(3, "system", 15_000, 20_000, "SPEAKER_01"), seg(4, "system", 20_000, 30_000, "SPEAKER_02"),
             seg(5, "system", 30_000, 40_000, "SPEAKER_01"), seg(6, "mic", 41_000, 45_000, "You")]
    m = TranscriptModel()
    m.apply_transcript(live)
    result = m.switch_to_final(final)
    assert result.changed == {("system", 30_000)}
    assert result.ops == [Reset()]
    assert [r.seq for r in m.lines] == [1, 2, 3, 4, 5, 6]  # the final file's own numbering


def test_swap_ignores_final_lines_with_no_live_counterpart():
    final = [seg(1, "system", 0, 10_000, "SPEAKER_01"), seg(2, "system", 50_000, 60_000, "SPEAKER_02")]
    old = [LineRow("system", 0, 10_000, "SPEAKER_01", None, "x", 1.0, 1)]
    new = [LineRow(e["source"], e["start_ms"], e["end_ms"], e["speaker"], None, "x", 1.0, e["seq"]) for e in final]
    assert changed_after_swap(old, new) == set()


def test_marks_and_gaps_survive_the_swap():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "mic", 1000, 2000, "You")])
    m.apply_marks([mark(1, 1500)])
    m.set_meta({"gaps": [{"source": "mic", "start_ms": 3000, "end_ms": 4000, "reason": "muted"}]})
    m.switch_to_final([seg(1, "mic", 1000, 2000, "You")])
    assert [type(r) for r in m.rows] == [LineRow, MarkRow, GapRow]


def test_reset_transcript_keeps_marks():
    m = TranscriptModel()
    m.apply_transcript([seg(1, "mic", 1000, 2000, "You")])
    m.apply_marks([mark(1, 1500)])
    assert m.reset_transcript() == [Reset()]
    assert [type(r) for r in m.rows] == [MarkRow]
    assert len(m.apply_transcript([seg(1, "mic", 1000, 2000, "You")])) == 1  # seq may be reused


def test_load_picks_final_when_done_and_live_otherwise(tmp_path):
    live = [("mic", 1000, 2000, "You", "hello."), ("system", 3000, 4000, "SPEAKER_01", "hi.")]
    final = [("mic", 1000, 2000, "You", "hello."), ("system", 3000, 4000, "SPEAKER_02", "hi.")]
    done = make_meeting(tmp_path, live, final=final, marks=[mark(1, 2500, label="note")],
                        tags=[{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Tagged"}])
    done.write_speakers({"SPEAKER_02": "Dana"})
    m = TranscriptModel.load(done)
    assert m.final and [r.display for r in m.lines] == ["You", "Dana"] and m.marks[0].label == "note"
    assert m.meeting_id == done.meeting_id and m.title == "Fixture"

    open_ = make_meeting(tmp_path / "other", live, tags=[
        {"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Tagged"}])
    m = TranscriptModel.load(open_)
    assert m.provisional and [r.display for r in m.lines] == ["You", "Tagged"]


def test_duration_and_clock():
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-11-09T11:00:03-06:00", "ended_at": "2026-11-09T11:42:30-06:00"})
    assert m.duration_ms == 2_547_000 and m.clock(12 * 60_000 + 41_000) == "11:12"
    assert fmt_elapsed(12 * 60_000 + 41_999) == "00:12:41"
    m.apply_transcript([{"type": "meeting_end", "ended_at": "x", "interrupted": True, "last_end_ms": 90_000}])
    assert m.duration_ms == 90_000
