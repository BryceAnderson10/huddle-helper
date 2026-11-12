"""tools/check_private.py: the private-terms guard for commits and gh bodies."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


# --- worktrees: no lab/ or .venv of their own, so they use the main checkout's ---


@pytest.fixture(autouse=True)
def no_outer_git(monkeypatch):
    """Inside a git hook, GIT_DIR and friends point at the real repo; keep them away."""
    for name in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(name)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def main_and_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("HUDDLE_PRIVATE_TERMS", raising=False)
    main, wt = tmp_path / "main", tmp_path / "wt"
    main.mkdir()
    git(main, "init", "-q")
    (main / "README").write_text("x\n", encoding="utf-8")
    git(main, "add", "README")
    git(main, "commit", "-q", "-m", "init")
    git(main, "worktree", "add", "-q", str(wt))
    (main / "lab").mkdir()
    (main / "lab" / "private_terms.txt").write_text("Zelda\n", encoding="utf-8")
    return main, wt


def test_worktree_reads_the_main_checkouts_terms(main_and_worktree):
    main, wt = main_and_worktree
    assert cp.terms_path(wt).resolve() == (main / "lab" / "private_terms.txt").resolve()


def test_worktree_own_terms_win(main_and_worktree):
    _, wt = main_and_worktree
    (wt / "lab").mkdir()
    (wt / "lab" / "private_terms.txt").write_text("Link\n", encoding="utf-8")
    assert cp.terms_path(wt) == wt / "lab" / "private_terms.txt"


def test_env_terms_win_over_the_main_checkout(main_and_worktree, monkeypatch, tmp_path):
    _, wt = main_and_worktree
    monkeypatch.setenv("HUDDLE_PRIVATE_TERMS", str(tmp_path / "other.txt"))
    assert cp.terms_path(wt) == tmp_path / "other.txt"


def test_outside_git_the_local_path_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("HUDDLE_PRIVATE_TERMS", raising=False)
    assert cp.terms_path(tmp_path) == tmp_path / "lab" / "private_terms.txt"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_hooks_find_the_main_checkouts_venv(main_and_worktree):
    main, wt = main_and_worktree
    exe = main / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    finder = Path(__file__).resolve().parents[1] / ".githooks" / "find-python.sh"
    out = subprocess.run(["sh", "-c", f'. "{finder.as_posix()}"; echo "$py"'], cwd=wt,
                         check=True, capture_output=True, encoding="utf-8").stdout.strip()
    assert Path(out).resolve() == exe.resolve()
