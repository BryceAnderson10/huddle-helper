"""Where the final transcript's wording differs from the live one (#44): a hint, never a verdict.

``live_wording(final_line, overlapping)`` takes one final line and the live lines that overlap it in time
on the same stream (``transcript_model.live_overlaps``) and returns ``None`` when their words are the same,
else the live wording and coarse flags for the words that differ. It never says which wording is right and
never changes either transcript.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .transcript_model import LineRow

NUMBER, NEGATION, NAME, DATE_TIME = "number", "negation", "name", "date_time"

_NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand "
    "million billion dozen half quarter".split())
_NEGATIONS = frozenset("not no never none nobody nothing nowhere neither nor without cannot "
                       "cant dont doesnt didnt wont wouldnt isnt arent wasnt werent shouldnt couldnt "
                       "hasnt havent hadnt aint".split())
_DATE_WORDS = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday today tomorrow yesterday tonight morning "
    "afternoon evening noon midnight o'clock a.m p.m pm weekend week weeks month months year years day days "
    "hour hours minute minutes".split())
_MONTHS = frozenset("January February March April May June July August September October November "
                    "December".split())  # only capitalised: "may" and "march" are also ordinary words
_CLOCK_OR_ORDINAL = re.compile(r"^\d{1,2}(:\d{2})?\s*([ap]\.?m\.?)$|^\d{1,2}:\d{2}$|^\d+(st|nd|rd|th)$")
_PRONOUN_I = frozenset("i i'm i'll i've i'd".split())
_PUNCT = ".,!?;:\"()[]“”"


def _tokens(text: str) -> list[str]:
    return text.split()


def _norm(token: str) -> str:
    return token.replace("’", "'").strip(_PUNCT).lower()


def _sentence_starts(tokens: list[str], line_starts: frozenset[int] = frozenset()) -> list[bool]:
    """Per token: does it start a sentence (first token, first of a line, or after . ? !)."""
    return [i == 0 or i in line_starts or tokens[i - 1].rstrip("\"')]”").endswith((".", "?", "!"))
            for i in range(len(tokens))]


def _live_tokens(lines: list[LineRow]) -> tuple[list[str], list[float], frozenset[int]]:
    """The live lines' words, each word's estimated time (spread evenly over its line), and the indexes of
    the words that start a line."""
    words: list[str] = []
    times: list[float] = []
    starts = set()
    for line in lines:
        toks = _tokens(line.text)
        starts.add(len(words))
        step = (line.end_ms - line.start_ms) / max(1, len(toks))
        words += toks
        times += [line.start_ms + step * (k + 0.5) for k in range(len(toks))]
    return words, times, frozenset(starts)


def _is_date_time(raw: str, norm: str) -> bool:
    return (norm in _DATE_WORDS or bool(_CLOCK_OR_ORDINAL.match(norm))
            or raw.strip(_PUNCT) in _MONTHS)


def _is_name_like(raw: str, norm: str, starts_sentence: bool) -> bool:
    word = raw.lstrip(_PUNCT + "'")
    return (not starts_sentence and word[:1].isupper() and norm not in _PRONOUN_I
            and not _is_date_time(raw, norm))


def _flags(changed: list[tuple[str, bool]]) -> list[str]:
    """Coarse kinds of the words that differ, in a fixed order. ``changed``: (word as written, whether it
    starts a sentence) from either side."""
    words = [(raw, _norm(raw), start) for raw, start in changed]
    checks = [
        (NUMBER, lambda raw, t, start: any(c.isdigit() for c in t) or t in _NUMBER_WORDS),
        (NEGATION, lambda raw, t, start: t.endswith("n't") or t.replace("'", "") in _NEGATIONS),
        (NAME, lambda raw, t, start: _is_name_like(raw, t, start)),
        (DATE_TIME, lambda raw, t, start: _is_date_time(raw, t)),
    ]
    return [flag for flag, check in checks if any(check(*w) for w in words)]


def live_wording(final: LineRow, overlapping: list[tuple[LineRow, int]]) -> dict | None:
    """The live wording of ``final`` when its words differ (case and punctuation aside), else ``None``.

    ``overlapping``: the live lines on its stream that overlap it in time, as ``live_overlaps`` gives them.
    A live line that runs past the final line's start or end also holds words of the neighbouring final
    line: live words left over at that edge (nothing in the final line stands in for them) whose estimated
    time (words spread evenly over their live line) falls outside this line are left to the neighbour. ``text`` is the live words that line up with this line; ``start_ms``/``end_ms`` span the live
    lines they come from."""
    if not overlapping:
        return None
    lines = [line for line, _ in overlapping]
    live_tok, live_ms, line_starts = _live_tokens(lines)
    final_tok = _tokens(final.text)
    ops = SequenceMatcher(a=[_norm(t) for t in live_tok], b=[_norm(t) for t in final_tok],
                          autojunk=False).get_opcodes()
    # live words at the edges with nothing standing in for them here, and timed outside this line
    if ops and ops[0][0] == "delete":
        _, i1, i2, j1, j2 = ops[0]
        while i1 < i2 and live_ms[i1] < final.start_ms:
            i1 += 1
        ops[0] = ("delete", i1, i2, j1, j2)
    if ops and ops[-1][0] == "delete":
        _, i1, i2, j1, j2 = ops[-1]
        while i2 > i1 and live_ms[i2 - 1] > final.end_ms:
            i2 -= 1
        ops[-1] = ("delete", i1, i2, j1, j2)
    ops = [op for op in ops if op[1] < op[2] or op[3] < op[4]]
    live_start, final_start = _sentence_starts(live_tok, line_starts), _sentence_starts(final_tok)
    changed: list[tuple[str, bool]] = []
    for op, i1, i2, j1, j2 in ops:
        if op != "equal":
            changed += [*zip(live_tok[i1:i2], live_start[i1:i2]), *zip(final_tok[j1:j2], final_start[j1:j2])]
    if not changed:
        return None
    return {"text": " ".join(live_tok[ops[0][1]:ops[-1][2]]), "start_ms": lines[0].start_ms,
            "end_ms": max(line.end_ms for line in lines), "flags": _flags(changed)}
