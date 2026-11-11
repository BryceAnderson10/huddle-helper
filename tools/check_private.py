"""Stop private terms (people's names, the company, meeting titles) from reaching git or GitHub.

    python tools/check_private.py --staged            # pre-commit: added lines of the staged diff
    python tools/check_private.py --commit-msg FILE   # commit-msg: the message, minus # lines
    python tools/check_private.py --claude-hook       # Claude Code PreToolUse: a gh command and its body files
    python tools/check_private.py FILE [FILE ...]     # any files, e.g. an issue body before posting

The terms live in ``lab/private_terms.txt`` (``lab/`` is not in git), or the file named by
``HUDDLE_PRIVATE_TERMS``. One term per line; ``#`` starts a comment. A term matches as a whole
word, ignoring case; prefix it with ``case:`` to match case only (``case:Mark`` passes "mark
the line"). Words of a multi-word term may be joined by spaces, hyphens or underscores, so
"Weekly Sync" also catches the folder name "weekly-sync".

Exit 1 on a hit (2 for ``--claude-hook``, which blocks the tool call), 0 otherwise. With no terms
file it warns and passes, so a fresh clone still commits.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TERMS = ROOT / "lab" / "private_terms.txt"
SEP = r"[\s_-]+"


@dataclass(frozen=True)
class Term:
    text: str
    pattern: re.Pattern


@dataclass(frozen=True)
class Hit:
    term: str
    line_no: int
    line: str
    where: str = ""


def parse_terms(text: str) -> list[Term]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        flags = re.IGNORECASE
        if line.startswith("case:"):
            line, flags = line[5:].strip(), 0
        body = SEP.join(re.escape(w) for w in re.split(SEP, line) if w)
        out.append(Term(line, re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", flags)))
    return out


def find(text: str, terms: list[Term], where: str = "") -> list[Hit]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for t in terms:
            if t.pattern.search(line):
                hits.append(Hit(t.text, n, line.strip(), f"{where}:{n}" if where else ""))
    return hits


def scan_diff(diff: str, terms: list[Term]) -> list[Hit]:
    """Added lines of a unified diff, located by file and new line number."""
    hits, path, new_no = [], "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
        elif line.startswith("+"):
            hits += [Hit(h.term, new_no, h.line, f"{path}:{new_no}") for h in find(line[1:], terms)]
            new_no += 1
        elif line.startswith(" "):
            new_no += 1
    return hits


def strip_git_comments(msg: str) -> str:
    return "\n".join(l for l in msg.splitlines() if not l.startswith("#"))


GH = re.compile(r"(?<![\w-])gh(?:\.exe)?\s")
BODY_FILE = re.compile(r"(?:--body-file|-F)(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)")


def scan_hook_event(event: dict, terms: list[Term]) -> list[Hit]:
    """A Bash/PowerShell tool call that runs ``gh``: its command text and any ``--body-file``."""
    command = (event.get("tool_input") or {}).get("command") or ""
    if not GH.search(command):
        return []
    hits = find(command, terms, "command")
    for m in BODY_FILE.finditer(command):
        name = m.group(1).strip("\"'")
        path = Path(name) if Path(name).is_absolute() else Path(event.get("cwd") or ".") / name
        if path.is_file() and name != "-":
            hits += find(path.read_text(encoding="utf-8", errors="replace"), terms, str(path))
    return hits


def load_terms() -> tuple[list[Term], Path]:
    path = Path(os.environ.get("HUDDLE_PRIVATE_TERMS") or DEFAULT_TERMS)
    if not path.is_file():
        return [], path
    return parse_terms(path.read_text(encoding="utf-8")), path


def report(hits: list[Hit]) -> None:
    for h in hits:
        print(f"{h.where or h.line_no}: private term {h.term!r}: {h.line[:120]}", file=sys.stderr)


def main(argv: list[str] | None = None, stdin: str | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    terms, path = load_terms()
    if not terms:
        print(f"check_private: no terms in {path}; nothing checked", file=sys.stderr)
        return 0
    if argv[:1] == ["--claude-hook"]:
        raw = sys.stdin.buffer.read().decode("utf-8") if stdin is None else stdin
        hits = scan_hook_event(json.loads(raw or "{}"), terms)
        if hits:
            report(hits)
            print("Blocked: replace names with Remote A/B/C, drop titles and IDs, then retry.",
                  file=sys.stderr)
            return 2
        return 0
    if argv[:1] == ["--staged"]:
        diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], cwd=ROOT,
                              capture_output=True, encoding="utf-8", errors="replace").stdout
        hits = scan_diff(diff, terms)
    elif argv[:1] == ["--commit-msg"]:
        msg = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
        hits = find(strip_git_comments(msg), terms, "commit message")
    elif argv:
        hits = [h for f in argv for h in
                find(Path(f).read_text(encoding="utf-8", errors="replace"), terms, f)]
    else:
        print(__doc__, file=sys.stderr)
        return 2
    report(hits)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
