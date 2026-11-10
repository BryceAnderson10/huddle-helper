"""Custom vocabulary (WP3): parse the user's term list, fit it to a token budget, build the prompt.

The prompt is one sentence, "This call is about A, B and C.", passed to Whisper as
``hotwords``. A plain comma list or "Glossary:" prefix made Whisper echo the list and retry
decodes (TODO.md, "Transcription quality"). Attendee names are deliberately never added here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable

TOKEN_BUDGET = 120  # Whisper's prompt limit is about 224 tokens; long prompts also slow every utterance
CHARS_PER_TOKEN = 3.5  # only used when no tokenizer is loaded; a little pessimistic on purpose


def parse_terms(text: str) -> list[str]:
    """One term per line. Blank lines and lines starting with ``#`` are ignored; the rest is
    trimmed. Duplicates (ignoring case) are dropped, keeping the first spelling."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def build_prompt(terms: Iterable[str]) -> str | None:
    """"This call is about A, B and C." or None when there are no terms."""
    items = [t.strip() for t in terms if t and t.strip()]
    if not items:
        return None
    if len(items) == 1:
        body = items[0]
    else:
        body = ", ".join(items[:-1]) + " and " + items[-1]
    return f"This call is about {body}."


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) if text else 0


def fit_budget(meeting_terms: Iterable[str], settings_terms: Iterable[str],
               count_tokens: Callable[[str], int] = estimate_tokens,
               budget: int = TOKEN_BUDGET) -> tuple[list[str], list[str]]:
    """Meeting terms first, then Settings terms, until the *built sentence* would pass
    ``budget`` tokens. Returns (used, dropped); everything from the first term that does not
    fit onward is dropped, so the cut is a single clear line in the list."""
    ordered: list[str] = []
    seen: set[str] = set()
    for term in [*meeting_terms, *settings_terms]:
        term = term.strip()
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            ordered.append(term)
    used: list[str] = []
    for i, term in enumerate(ordered):
        if count_tokens(build_prompt([*used, term]) or "") > budget:
            return used, ordered[i:]
        used.append(term)
    return used, []


@dataclass(frozen=True)
class VocabPlan:
    used: list[str]
    dropped: list[str]
    exact: bool  # True when the model's own tokenizer did the counting

    @property
    def total(self) -> int:
        return len(self.used) + len(self.dropped)

    @property
    def prompt(self) -> str | None:
        return build_prompt(self.used)


def plan(meeting_terms: Iterable[str], settings_terms: Iterable[str],
         count_tokens: Callable[[str], int] | None = None) -> VocabPlan:
    used, dropped = fit_budget(meeting_terms, settings_terms, count_tokens or estimate_tokens)
    return VocabPlan(used, dropped, exact=count_tokens is not None)


def counter_for(transcriber: object) -> Callable[[str], int] | None:
    """The transcriber's tokenizer, if it has one (fakes in tests do not)."""
    fn = getattr(transcriber, "count_tokens", None)
    return fn if callable(fn) else None


def summary_lines(p: VocabPlan) -> tuple[str, str]:
    """(count line, dropped line) for the dialogs; the second is empty when nothing is dropped."""
    about = "" if p.exact else "about "
    count = f"{about}{len(p.used)} of {p.total} terms used" if p.total else "No terms yet"
    if not p.dropped:
        return count, ""
    shown = ", ".join(p.dropped[:12]) + (f" and {len(p.dropped) - 12} more" if len(p.dropped) > 12 else "")
    return count, f"Left out (over the limit): {shown}"
