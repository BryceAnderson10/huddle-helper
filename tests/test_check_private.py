"""tools/check_private.py: the private-terms guard for commits and gh bodies."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_private as cp  # noqa: E402


def terms(*lines):
    return cp.parse_terms("\n".join(lines))


def hits(text, *lines):
    return [h.term for h in cp.find(text, terms(*lines))]


def test_whole_word_and_case_insensitive_by_default():
    assert hits("asked zelda about it", "Zelda") == ["Zelda"]
    assert hits("ZELDA's line", "Zelda") == ["Zelda"]
    assert hits("zeldas and azelda", "Zelda") == []


def test_case_prefix_matches_case_sensitively():
    assert hits("mark the line", "case:Mark") == []
    assert hits("then Mark said", "case:Mark") == ["Mark"]


def test_multi_word_term_matches_across_spaces_hyphens_and_underscores():
    for text in ("the Weekly Sync call", "2026_0900_weekly-sync", "weekly_sync"):
        assert hits(text, "Weekly Sync") == ["Weekly Sync"], text


def test_comments_and_blank_lines_are_not_terms():
    assert [t.text for t in terms("# names", "", "  Zelda  ")] == ["Zelda"]


def test_hits_carry_line_numbers():
    found = cp.find("ok\nZelda here\nok", terms("Zelda"))
    assert [(h.line_no, h.line) for h in found] == [(2, "Zelda here")]


def test_added_lines_of_a_diff_only():
    diff = "\n".join([
        "diff --git a/TODO.md b/TODO.md",
        "+++ b/TODO.md",
        "@@ -1,0 +5,2 @@",
        "-Zelda was removed",
        "+Remote A said",
        "+Zelda said",
    ])
    found = cp.scan_diff(diff, terms("Zelda"))
    assert [(h.where, h.line) for h in found] == [("TODO.md:6", "Zelda said")]


def test_commit_message_ignores_git_comment_lines():
    msg = "Test #6 notes\n# On branch Zelda\n"
    assert cp.find(cp.strip_git_comments(msg), terms("Zelda")) == []


def test_hook_blocks_gh_command_text(tmp_path):
    event = {"tool_input": {"command": 'gh issue comment 9 --body "Zelda spoke"'}}
    assert [h.term for h in cp.scan_hook_event(event, terms("Zelda"))] == ["Zelda"]


def test_hook_reads_body_files(tmp_path):
    body = tmp_path / "c9.md"
    body.write_text("Remote A and Zelda", encoding="utf-8")
    for flag in (f"--body-file {body}", f'--body-file="{body}"', f"-F {body}"):
        event = {"tool_input": {"command": f"gh issue create --title x {flag}"}}
        assert [h.term for h in cp.scan_hook_event(event, terms("Zelda"))] == ["Zelda"], flag


def test_hook_ignores_commands_that_do_not_call_gh():
    event = {"tool_input": {"command": "grep -i Zelda lab/src/notes.txt"}}
    assert cp.scan_hook_event(event, terms("Zelda")) == []


def test_hook_main_exits_2_on_a_hit(tmp_path, monkeypatch, capsys):
    tf = tmp_path / "terms.txt"
    tf.write_text("Zelda\n", encoding="utf-8")
    monkeypatch.setenv("HUDDLE_PRIVATE_TERMS", str(tf))
    event = json.dumps({"tool_input": {"command": "gh issue comment 1 --body Zelda"}})
    assert cp.main(["--claude-hook"], stdin=event) == 2
    assert "private term" in capsys.readouterr().err


def test_missing_terms_file_warns_and_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HUDDLE_PRIVATE_TERMS", str(tmp_path / "absent.txt"))
    assert cp.main(["--claude-hook"], stdin='{"tool_input": {"command": "gh issue list"}}') == 0
    assert "absent.txt" in capsys.readouterr().err
