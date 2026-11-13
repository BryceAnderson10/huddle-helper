import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from huddle_helper import cli, export
from huddle_helper.transcript_model import TranscriptModel

from .helpers import make_meeting

DATA = Path(__file__).parent / "data" / "export"
TZ = timezone(timedelta(hours=-6))
START = datetime(2026, 11, 9, 11, 0, 3, tzinfo=TZ)
END = "2026-11-09T11:42:30-06:00"

# Invented text only (the two examples from docs/LIVE_VIEW.md).
LIVE = [
    ("mic", 1200, 4850, "You", "Let's start with the launch checklist."),
    ("system", 5100, 9020, "SPEAKER_01", "Legal still needs one more day."),
    ("system", 378_000, 380_500, "SPEAKER_02", "OK, decision: we ship Friday."),
]
MUTED = {"source": "mic", "start_ms": 305_000, "end_ms": 365_000, "reason": "muted"}
DECISION = {"type": "mark", "id": 1, "kind": "decision", "at_ms": 381_000, "via": "hotkey"}


def weekly(tmp_path, marks=(DECISION,), final=True, **kw):
    folder = make_meeting(tmp_path, LIVE, final=LIVE if final else None, marks=list(marks), gaps=[MUTED],
                          title="Weekly sync", started_at=START, **kw)
    folder.update_meta(ended_at=END)
    if final:
        folder.write_speakers({"SPEAKER_01": "Dana", "SPEAKER_02": "Eric"})
    return folder


def model(folder) -> TranscriptModel:
    return TranscriptModel.load(folder)


def test_md_matches_the_live_view_example(tmp_path):
    marks = [DECISION,
             {"type": "mark", "id": 2, "kind": "bookmark", "at_ms": 512_000},
             {"type": "mark", "id": 3, "kind": "action", "at_ms": 542_000},
             {"type": "mark_label", "id": 1, "label": "Ship Friday"},
             {"type": "mark_label", "id": 3, "label": "Dana to chase legal"}]
    text = export.render(model(weekly(tmp_path, marks=marks)), "md")
    assert text == (DATA / "weekly_sync.md").read_text(encoding="utf-8")


def with_label(marks):
    return [*marks, {"type": "mark_label", "id": 1, "label": "Ship Friday"}]


def test_txt_matches_the_live_view_example(tmp_path):
    folder = weekly(tmp_path, marks=with_label([DECISION]))
    assert export.render(model(folder), "txt", crlf=False) == (DATA / "weekly_sync.txt").read_text(encoding="utf-8")


def test_txt_uses_windows_line_endings_by_default_and_md_does_not(tmp_path):
    m = model(weekly(tmp_path, marks=with_label([DECISION])))
    txt = export.render(m, "txt")
    assert txt.count("\r\n") == txt.count("\n") > 5 and "\n" not in txt.replace("\r\n", "")
    assert "\r" not in export.render(m, "md")
    out = tmp_path / "out.txt"
    export.write_export(out, txt)
    raw = out.read_bytes()
    assert raw.count(b"\r\n") == txt.count("\r\n") and b"\r\r" not in raw
    assert "11:00–".encode("utf-8") in raw


def test_live_only_export_carries_the_provisional_banner(tmp_path):
    folder = weekly(tmp_path, final=False, marks=[])
    folder.update_meta(status="processing")
    m = model(folder)
    txt = export.render(m, "txt", crlf=False)
    assert txt.startswith("Provisional: speaker labels may change\n\nWeekly sync\n")
    assert "[00:00:05] SPEAKER_01: Legal" in txt  # live labels, no speakers.json
    md = export.render(m, "md")
    assert md.startswith("> Provisional: speaker labels may change\n\n# Weekly sync\n")
    assert "Provisional" not in export.render(model(weekly(tmp_path / "b")), "md")


def test_live_export_applies_tags(tmp_path):
    tags = [{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Dana"}]
    folder = make_meeting(tmp_path, LIVE, tags=tags, title="Tagged", started_at=START)
    txt = export.render(model(folder), "txt", crlf=False)
    assert "[00:00:05] Dana:" in txt and "Speakers: You, Dana, SPEAKER_02" in txt


def test_interrupted_meeting(tmp_path):
    folder = weekly(tmp_path, marks=[])
    folder.update_meta(status="interrupted", clean_exit=False, ended_at="2026-11-09T18:00:00-06:00")
    # recovery appended meeting_end with interrupted and the last audio time
    for path in (folder.live_path, folder.final_path):
        lines = path.read_text(encoding="utf-8").splitlines()
        end = json.loads(lines[-1])
        end.update(interrupted=True, last_end_ms=600_000)
        lines[-1] = json.dumps(end)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    m = model(folder)
    txt = export.render(m, "txt", crlf=False)
    assert "2026-11-09, 11:00–11:10 (10 min)" in txt  # from the last audio, not the recovery time
    assert export.INTERRUPTED in txt
    assert f"> {export.INTERRUPTED}" in export.render(m, "md")


def test_a_recovered_meeting_that_finished_its_final_pass_is_still_marked_recovered(tmp_path):
    # recover sets clean_exit false; the final pass then sets status complete (GLOSSARY "Recovered")
    folder = weekly(tmp_path, marks=[])
    folder.update_meta(clean_exit=False)
    m = model(folder)
    assert export.INTERRUPTED in export.render(m, "txt", crlf=False)
    assert f"> {export.INTERRUPTED}" in export.render(m, "md")


def test_a_meeting_that_ended_with_stop_is_not_marked_recovered(tmp_path):
    assert export.INTERRUPTED not in export.render(model(weekly(tmp_path, marks=[])), "txt", crlf=False)


def test_no_marks_means_no_bookmarks_section(tmp_path):
    md = export.render(model(weekly(tmp_path, marks=[])), "md")
    assert "## Bookmarks" not in md and "bookmark" not in md
    assert "## Transcript" in md


def test_deleted_mark_is_not_exported(tmp_path):
    folder = weekly(tmp_path, marks=[DECISION, {"type": "mark_delete", "id": 1}])
    assert "Decision" not in export.render(model(folder), "txt")


def test_mark_in_a_pause_goes_before_the_next_paragraph(tmp_path):
    lines = [("mic", 1000, 3000, "You", "First."), ("system", 10_000, 12_000, "SPEAKER_01", "Second.")]
    marks = [{"type": "mark", "id": 1, "kind": "bookmark", "at_ms": 6000},  # in the pause
             {"type": "mark", "id": 2, "kind": "question", "at_ms": 11_000},  # inside "Second."
             {"type": "mark", "id": 3, "kind": "bookmark", "at_ms": 500}]  # before everything
    folder = make_meeting(tmp_path, lines, marks=marks, title="Pause", started_at=START)
    body = export.render(model(folder), "txt", crlf=False).split("\n\n")[-1].splitlines()
    assert body == ["---- Bookmark 00:00:00 ----", "[00:00:01] You: First.", "---- Bookmark 00:00:06 ----",
                    "[00:00:10] SPEAKER_01: Second.", "---- Question 00:00:11 ----"]


def test_mark_in_a_mute_goes_after_the_gap_line(tmp_path):
    lines = [("mic", 1000, 3000, "You", "First."), ("mic", 70_000, 72_000, "You", "Second.")]
    gap = {"source": "mic", "start_ms": 5000, "end_ms": 65_000, "reason": "muted"}
    marks = [{"type": "mark", "id": 1, "kind": "decision", "at_ms": 30_000}]
    folder = make_meeting(tmp_path, lines, marks=marks, gaps=[gap], title="Mute", started_at=START)
    txt = export.render(model(folder), "txt", crlf=False)
    assert ("(00:00:05-00:01:05 you muted your mic in the recording, 1 min)\n---- Decision 00:00:30 ----\n"
            "[00:01:10] You: Second.") in txt
    # the muted gap also separates the two You lines even though join is on
    assert txt.count("You:") == 2


def test_join_merges_same_speaker_and_no_join_does_not(tmp_path):
    lines = [("system", 1000, 2000, "SPEAKER_01", "One."), ("system", 3000, 4000, "SPEAKER_01", "Two."),
             ("mic", 5000, 6000, "You", "Three."), ("system", 7000, 8000, "SPEAKER_01", "Four.")]
    folder = make_meeting(tmp_path, lines, title="Join", started_at=START)
    m = model(folder)
    joined = export.render(m, "txt", crlf=False)
    assert "[00:00:01] SPEAKER_01: One. Two.\n[00:00:05] You: Three.\n[00:00:07] SPEAKER_01: Four." in joined
    plain = export.render(m, "txt", join=False, crlf=False)
    assert "[00:00:03] SPEAKER_01: Two." in plain and plain.count("SPEAKER_01:") == 3


def test_join_uses_the_person_not_the_label(tmp_path):
    lines = [("system", 1000, 2000, "SPEAKER_01", "One."), ("system", 3000, 4000, "SPEAKER_03", "Two.")]
    tags = [{"type": "tag", "scope": "speaker", "label": "SPEAKER_01", "name": "Eric"},
            {"type": "tag", "scope": "speaker", "label": "SPEAKER_03", "name": "Eric"}]
    folder = make_meeting(tmp_path, lines, tags=tags, title="Join", started_at=START)
    assert "[00:00:01] Eric: One. Two." in export.render(model(folder), "txt", crlf=False)


def test_clock_times(tmp_path):
    m = model(weekly(tmp_path, marks=with_label([DECISION])))
    txt = export.render(m, "txt", times="clock", crlf=False)
    # 11:00:03 start; 1.2 s, 5.1 s, 305 s, 378 s, 381 s after it
    assert "[11:00] You:" in txt and "[11:06] Eric:" in txt
    assert "(11:05-11:06 you muted your mic in the recording, 1 min)" in txt
    assert "---- Decision 11:06: Ship Friday ----" in txt
    md = export.render(m, "md", times="clock")
    assert "- [11:06 Ship Friday](#bookmark-1)" in md and "**11:06 Eric:**" in md


def test_other_gaps_and_custom_kinds(tmp_path):
    gap = {"source": "system", "start_ms": 20_000, "end_ms": 24_000, "reason": "default audio device changed"}
    marks = [{"type": "mark", "id": 1, "kind": "risk", "at_ms": 30_000}]
    folder = make_meeting(tmp_path, LIVE[:2], marks=marks, gaps=[gap], title="Gaps", started_at=START)
    folder.update_meta(mark_kinds={"risk": "Risk"})
    m = model(folder)
    assert "(00:00:20-00:00:24 no audio from the call (default audio device changed), 4 s)" in \
        export.render(m, "txt", crlf=False)
    md = export.render(m, "md")
    assert "### Risk\n- [00:00:30](#bookmark-1)" in md and "> **Risk 00:00:30**" in md


def test_group_order_is_decisions_actions_questions_then_bookmarks_then_custom(tmp_path):
    marks = [{"type": "mark", "id": i, "kind": k, "at_ms": i * 1000}
             for i, k in enumerate(["risk", "bookmark", "question", "action", "decision"], 1)]
    folder = make_meeting(tmp_path, LIVE[:1], marks=marks, title="Order", started_at=START)
    folder.update_meta(mark_kinds={"risk": "Risk"})
    md = export.render(model(folder), "md")
    heads = [line[4:] for line in md.splitlines() if line.startswith("### ")]
    assert heads == ["Decisions", "Action items", "Questions", "Bookmarks", "Risk"]


def test_markdown_escapes_special_characters(tmp_path):
    lines = [("mic", 1000, 2000, "You", "use snake_case and *stars* [here] _x_")]
    folder = make_meeting(tmp_path, lines, title="A_B", started_at=START)
    md = export.render(model(folder), "md")
    assert r"use snake_case and \*stars\* \[here\] \_x\_" in md and "# A_B" in md


def test_empty_meeting_still_exports(tmp_path):
    folder = make_meeting(tmp_path, [], title="Empty", started_at=START)
    assert "(no transcript lines)" in export.render(model(folder), "txt", crlf=False)
    assert "*No transcript lines.*" in export.render(model(folder), "md")


def test_bad_options_raise(tmp_path):
    m = model(weekly(tmp_path))
    with pytest.raises(ValueError):
        export.render(m, "pdf")
    with pytest.raises(ValueError):
        export.render(m, "md", times="soon")


def test_cli_export_writes_named_file_in_cwd_and_honours_options(tmp_path, monkeypatch, capsys):
    folder = weekly(tmp_path / "meetings", marks=with_label([DECISION]))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert cli.main(["export", str(folder.path), "--format", "txt"]) == 0
    out = work / f"{folder.meeting_id}.txt"
    assert out.read_bytes().decode("utf-8").replace("\r\n", "\n") == (DATA / "weekly_sync.txt").read_text("utf-8")
    assert b"\r\n" in out.read_bytes()
    target = tmp_path / "sub" / "x.md"
    assert cli.main(["export", str(folder.path), "--format", "md", "--out", str(target), "--times", "clock",
                     "--no-join"]) == 0
    assert "**11:06 Eric:**" in target.read_text(encoding="utf-8")
    assert cli.main(["export", str(tmp_path / "nope")]) == 1


def test_cli_export_is_registered_in_the_real_entry_point(tmp_path):
    folder = weekly(tmp_path / "meetings")
    out = tmp_path / "e.md"
    done = subprocess.run([sys.executable, "-m", "huddle_helper", "export", str(folder.path), "--out", str(out)],
                          capture_output=True, text=True, env={**__import__("os").environ,
                                                               "HUDDLE_HELPER_CONFIG_DIR": str(tmp_path / "c"),
                                                               "HUDDLE_HELPER_DATA_DIR": str(tmp_path / "d")})
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8").startswith("# Weekly sync")


# -- marks and muted periods together (goldens) ---------------------------------------------------------
STAND_LINES = [
    ("system", 15_000, 18_000, "SPEAKER_01", "Morning, everyone."),
    ("system", 19_000, 23_000, "SPEAKER_01", "First item is the budget."),
    ("mic", 24_000, 26_000, "You", "Agreed, go ahead."),
    ("system", 90_000, 95_000, "SPEAKER_02", "The vendor needs an answer by Thursday."),
    ("mic", 96_000, 98_000, "You", "I will reply today."),
]
STAND_GAPS = [
    {"source": "mic", "start_ms": 0, "end_ms": 12_250, "reason": "muted"},  # started muted
    {"source": "system", "start_ms": 40_000, "end_ms": 44_000, "reason": "default audio device changed"},
    {"source": "mic", "start_ms": 60_000, "end_ms": 85_250, "reason": "muted"},
]
STAND_MARKS = [
    {"type": "mark", "id": 1, "kind": "bookmark", "at_ms": 5_000, "via": "button"},  # before the first line
    {"type": "mark", "id": 2, "kind": "bookmark", "at_ms": 21_000, "via": "hotkey"},  # inside a line
    {"type": "mark", "id": 3, "kind": "action", "at_ms": 30_000, "via": "hotkey"},  # in a pause
    {"type": "mark", "id": 4, "kind": "decision", "at_ms": 62_000, "via": "tray"},  # inside the muted period
    {"type": "mark", "id": 5, "kind": "question", "at_ms": 90_000, "via": "line", "seq": 4},  # a line's own start
    {"type": "mark", "id": 6, "kind": "bookmark", "at_ms": 99_000, "via": "hotkey"},  # after the last line
    {"type": "mark", "id": 7, "kind": "decision", "at_ms": 10_000, "via": "hotkey"},  # deleted again
    {"type": "mark_label", "id": 2, "label": "Budget *draft*"},
    {"type": "mark_kind", "id": 2, "kind": "decision"},  # retyped by a second press
    {"type": "mark_label", "id": 3, "label": "Reply to the vendor"},
    {"type": "mark_label", "id": 5, "label": "Thursday?"},
    {"type": "mark_label", "id": 5, "label": ""},  # cleared
    {"type": "mark_delete", "id": 7},
    {"type": "mark", "id": 8, "kind": "weird kind", "at_ms": 99_500, "via": "hotkey"},  # unknown kind
]


def standup(tmp_path):
    folder = make_meeting(tmp_path, STAND_LINES, final=STAND_LINES, marks=STAND_MARKS, gaps=STAND_GAPS,
                          title="Daily standup", started_at=START)
    folder.update_meta(ended_at="2026-11-09T11:02:00-06:00")
    folder.write_speakers({"SPEAKER_01": "Dana", "SPEAKER_02": "Eric"})
    return folder


def test_marks_and_muted_periods_golden_markdown(tmp_path):
    text = export.render(model(standup(tmp_path)), "md")
    assert text == (DATA / "standup.md").read_text(encoding="utf-8")


def test_marks_and_muted_periods_golden_text(tmp_path):
    text = export.render(model(standup(tmp_path)), "txt", crlf=False)
    assert text == (DATA / "standup.txt").read_text(encoding="utf-8")


def test_the_export_reads_the_same_rows_as_the_window(tmp_path):
    """The export and the window read one model: rows by at_ms (a tie: line, gap, mark), ids kept."""
    m = model(standup(tmp_path))
    assert [r.at_ms for r in m.rows] == sorted(r.at_ms for r in m.rows)
    assert [r.id for r in m.marks] == [1, 2, 3, 4, 5, 6, 8] and m.marks[-1].kind == "bookmark"
