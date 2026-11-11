"""Word error rate of a transcript against a reference text corrected by ear (lab tool).

    python tools/wer.py HYP [HYP ...] [--ref lab/ref/<meeting>.txt] [--source system|mic]
                                      [--from-s N] [--until-s N] [--show 15]

HYP is a ``transcript*.jsonl`` (lines of type "segment") or a ``lab/retx/<meeting>__<tag>.json``
(``tools/retx_lab.py run``). The reference is plain text in ``lab/ref/<meeting>.txt`` (never in
git); the meeting is the transcript's folder name, or the part before ``__`` of a retx file.
Lines of the reference starting with ``#`` are comments, except ``# until_s: 300`` and
``# from_s: 0``, which say what part of the meeting the reference covers (the options override
them); only transcript lines that start inside that part are scored.

Both sides are normalised the same way before comparing (``normalize``): lower case, punctuation
stripped, hyphens as spaces, apostrophes dropped, numbers written as words where that is simple
(integers, decimals, ordinals, "$5", "50%"). "2026" becomes "two thousand twenty six", so a year
spoken "twenty twenty six" still counts as an error; "okay" and "alright" are folded to "ok" and
"all right". Speaker labels are not looked at.

WER = (substitutions + deletions + insertions) / words in the reference.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REF_DIR = Path("lab/ref")

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORDINALS = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
             "nine": "ninth", "twelve": "twelfth"}
_FOLD = {"okay": ["ok"], "alright": ["all", "right"]}
MAX_NUMBER_DIGITS = 9  # longer runs of digits (phone numbers, ids) are read digit by digit


def number_words(n: int) -> list[str]:
    """0..999,999,999 as words, without "and": 1234 -> one thousand two hundred thirty four."""
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (number_words(n % 100) if n % 100 else [])
    for size, name in ((1_000_000, "million"), (1_000, "thousand")):
        if n >= size:
            return number_words(n // size) + [name] + (number_words(n % size) if n % size else [])
    raise ValueError(n)


def _digits(text: str) -> list[str]:
    return [_ONES[int(c)] for c in text]


def _integer(text: str) -> list[str]:
    text = text.replace(",", "")
    if len(text) > MAX_NUMBER_DIGITS or (len(text) > 1 and text.startswith("0")):
        return _digits(text)
    return number_words(int(text))


def _ordinal(text: str) -> list[str]:
    words = _integer(text)
    last = words[-1]
    if last in _ORDINALS:
        words[-1] = _ORDINALS[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return words


_NUMBER = re.compile(r"""
    (?<![a-z0-9])
    (?P<dollar>\$)?
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)
    (?:\.(?P<frac>\d+))?
    (?:(?P<ord>st|nd|rd|th)|(?P<pct>\s?%))?
    (?![a-z0-9])
""", re.X)


def _spell(m: re.Match) -> str:
    if m["ord"] and not m["frac"]:
        words = _ordinal(m["int"])
    else:
        words = _integer(m["int"])
        if m["frac"]:
            words += ["point"] + _digits(m["frac"])
    if m["dollar"]:
        words.append("dollar" if words == ["one"] else "dollars")
    if m["pct"]:
        words.append("percent")
    return " " + " ".join(words) + " "


def normalize(text: str) -> list[str]:
    """The words of ``text`` as compared: see the module docstring."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = _NUMBER.sub(_spell, text)
    text = text.replace("&", " and ").replace("'", "")
    out: list[str] = []
    for word in re.sub(r"[^a-z0-9]+", " ", text).split():
        out.extend(_FOLD.get(word, [word]))
    return out


# -- edit distance ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Score:
    ref_words: int
    hyp_words: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        """Errors per reference word; with an empty reference, 0.0 if the transcript is empty too, else inf."""
        if self.ref_words:
            return self.errors / self.ref_words
        return 0.0 if not self.hyp_words else float("inf")


def _ids(a: list[str], b: list[str]) -> tuple[np.ndarray, np.ndarray]:
    table: dict[str, int] = {}
    return (np.array([table.setdefault(w, len(table)) for w in a], dtype=np.int64),
            np.array([table.setdefault(w, len(table)) for w in b], dtype=np.int64))


def _rows(ref: np.ndarray, hyp: np.ndarray):
    """Rows of the edit-distance table, one per reference word (row 0 first)."""
    steps = np.arange(len(hyp) + 1, dtype=np.int64)
    row = steps.copy()
    yield row
    for i, word in enumerate(ref, 1):
        new = np.empty_like(row)
        new[0] = i
        np.minimum(row[1:] + 1, row[:-1] + (hyp != word), out=new[1:])
        row = np.minimum.accumulate(new - steps) + steps  # insertions: new[j] <= new[j-1] + 1
        yield row


def distance(a: list[str], b: list[str]) -> int:
    """Fewest word substitutions, deletions and insertions turning ``a`` into ``b``."""
    if not a or not b:
        return len(a) + len(b)
    ref, hyp = _ids(a, b)
    row = None
    for row in _rows(ref, hyp):
        pass
    return int(row[-1])


def align(ref: list[str], hyp: list[str]) -> list[tuple[str, str | None, str | None]]:
    """One best alignment as (op, ref word, hyp word); op is "ok", "sub", "del" or "ins"."""
    r, h = _ids(ref, hyp)
    table = np.stack(list(_rows(r, h)))
    ops: list[tuple[str, str | None, str | None]] = []
    i, j = len(ref), len(hyp)
    while i or j:
        here = table[i, j]
        if i and j and ref[i - 1] == hyp[j - 1] and table[i - 1, j - 1] == here:
            ops.append(("ok", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i and j and table[i - 1, j - 1] + 1 == here:
            ops.append(("sub", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i and table[i - 1, j] + 1 == here:
            ops.append(("del", ref[i - 1], None))
            i -= 1
        else:
            ops.append(("ins", None, hyp[j - 1]))
            j -= 1
    ops.reverse()
    return ops


def score(ref: list[str], hyp: list[str]) -> Score:
    kinds = Counter(op for op, _r, _h in align(ref, hyp))
    return Score(len(ref), len(hyp), kinds["sub"], kinds["del"], kinds["ins"])


def wer(ref_text: str, hyp_text: str) -> Score:
    return score(normalize(ref_text), normalize(hyp_text))


# -- files --------------------------------------------------------------------------------------
def meeting_of(path: Path) -> str:
    """The meeting a transcript belongs to: ``<meeting>__<tag>.json`` or ``<meeting>/transcript*.jsonl``."""
    path = Path(path)
    if path.suffix == ".json" and "__" in path.stem:
        return path.stem.split("__")[0]
    return path.resolve().parent.name


def load_lines(path: Path) -> list[dict]:
    """Transcript lines (dicts with text, start_ms and source), in file order."""
    path = Path(path)
    if path.suffix == ".jsonl":
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if r.get("type") == "segment"]
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


def hyp_text(lines: list[dict], source: str | None = None, from_s: float | None = None,
             until_s: float | None = None) -> str:
    """The lines' text in time order, limited to one source and to lines starting in [from_s, until_s)."""
    kept = [r for r in lines
            if (source is None or r.get("source") == source)
            and (from_s is None or r["start_ms"] >= from_s * 1000)
            and (until_s is None or r["start_ms"] < until_s * 1000)]
    kept.sort(key=lambda r: r["start_ms"])  # stable: equal starts keep file order
    return " ".join(r.get("text", "") for r in kept)


_RANGE = re.compile(r"#\s*(from_s|until_s)\s*:\s*([0-9.]+)\s*$")


def load_ref(path: Path) -> tuple[str, dict[str, float]]:
    """(text, {"from_s": ..., "until_s": ...} as far as the file says)."""
    text, span = [], {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            m = _RANGE.match(line.strip())
            if m:
                span[m[1]] = float(m[2])
            continue
        text.append(line)
    return "\n".join(text), span


def ref_path(meeting: str, ref_dir: Path = REF_DIR) -> Path:
    return Path(ref_dir) / f"{meeting}.txt"


def score_file(hyp: Path, ref: Path | None = None, source: str | None = None,
               from_s: float | None = None, until_s: float | None = None,
               ref_dir: Path = REF_DIR) -> tuple[Score, list] | None:
    """Score one transcript; None when its meeting has no reference file."""
    ref = Path(ref) if ref else ref_path(meeting_of(hyp), ref_dir)
    if not ref.exists():
        return None
    text, span = load_ref(ref)
    from_s = span.get("from_s") if from_s is None else from_s
    until_s = span.get("until_s") if until_s is None else until_s
    ops = align(normalize(text), normalize(hyp_text(load_lines(hyp), source, from_s, until_s)))
    kinds = Counter(op for op, _r, _h in ops)
    words = sum(1 for op, _r, _h in ops if op != "ins")
    return Score(words, len(ops) - kinds["del"], kinds["sub"], kinds["del"], kinds["ins"]), ops


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("hyp", nargs="+", type=Path)
    ap.add_argument("--ref", type=Path, default=None, help="reference text (default lab/ref/<meeting>.txt)")
    ap.add_argument("--ref-dir", type=Path, default=REF_DIR)
    ap.add_argument("--source", choices=["system", "mic"], default=None)
    ap.add_argument("--from-s", type=float, default=None)
    ap.add_argument("--until-s", type=float, default=None)
    ap.add_argument("--show", type=int, default=0, help="print the N most frequent errors")
    a = ap.parse_args(argv)
    missing = 0
    for hyp in a.hyp:
        got = score_file(hyp, a.ref, a.source, a.from_s, a.until_s, a.ref_dir)
        if got is None:
            missing += 1
            print(f"{hyp}: no reference transcript ({a.ref or ref_path(meeting_of(hyp), a.ref_dir)})")
            continue
        s, ops = got
        print(f"{hyp}: WER {s.wer:.1%}  ({s.substitutions} substituted, {s.deletions} missing, "
              f"{s.insertions} added; {s.ref_words} reference words, {s.hyp_words} transcript words)")
        if a.show:
            errors = Counter((op, r, h) for op, r, h in ops if op != "ok")
            for (op, r, h), n in errors.most_common(a.show):
                print(f"   {n:3d}  {op}  {r or '-'} -> {h or '-'}")
    return 1 if missing == len(a.hyp) else 0


if __name__ == "__main__":
    sys.exit(main())
