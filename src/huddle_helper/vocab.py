"""Custom vocabulary (WP3): parse the user's term list, fit it to a token budget, build the prompt.

The prompt is one sentence, "This call is about A, B and C.", passed to Whisper as
``hotwords``. A plain comma list or "Glossary:" prefix made Whisper echo the list and retry
decodes (TODO.md, "Transcription quality"). Attendee names are deliberately never added here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
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


# -- one term from a line (L37) ---------------------------------------------------------------------------
TERM_MAX_CHARS = 80  # a vocabulary term is a word or short phrase; anything longer is a sentence picked by mistake
_QUOTES = " \"'“”‘’()[]{}<>"  # brackets and quotes trimmed from both ends of selected words


def clean_term(raw: str) -> str:
    """Selected words as a term: line breaks and runs of spaces become one space; quotes and brackets are
    trimmed from both ends, and ``. , ; : ! ?`` from the end ("Quodium." -> "Quodium"). A leading ``.`` is kept
    (``.NET``)."""
    text = " ".join((raw or "").split())
    while True:
        cut = text.strip(_QUOTES).rstrip(" .,;:!?").lstrip(" ,;:!?")
        if cut == text:
            return text
        text = cut


@dataclass(frozen=True)
class TermCheck:
    term: str  # the cleaned term
    ok: bool  # can be added
    message: str  # what to tell the user ("" when there is nothing to say)
    level: str  # "ok", "warn" (can be added, but would not be used) or "error"
    fits: bool = True  # False: it would be left out by the token budget


def check_new_term(raw: str, existing: Iterable[str],
                   plan: Callable[[list[str]], "VocabPlan"] | None = None) -> TermCheck:
    """Can ``raw`` (as typed) be added to the Settings vocabulary ``existing``? Same rules as ``parse_terms``
    (one line, not a ``#`` comment, not a duplicate ignoring case) plus a length limit. ``plan(terms)`` is
    ``AppController.plan_vocabulary``: when the new term would be left out by the token budget the check is still
    ``ok`` but says so (level "warn")."""
    term = clean_term(raw)
    if not term:
        return TermCheck(term, False, "Type the term as it should be written.", "error")
    if term.startswith("#"):
        return TermCheck(term, False, "A term cannot start with # (those lines are comments).", "error")
    if len(term) > TERM_MAX_CHARS:
        return TermCheck(term, False, f"Too long for a term ({len(term)} of {TERM_MAX_CHARS} characters). "
                                      "Pick just the word or short phrase.", "error")
    have = list(existing)
    for t in have:
        if t.casefold() == term.casefold():
            return TermCheck(term, False, f"“{t}” is already in the vocabulary.", "error")
    if plan is not None:
        try:
            p = plan([*have, term])
        except Exception:  # noqa: BLE001 - the budget note is optional
            p = None
        if p is not None and any(d.casefold() == term.casefold() for d in p.dropped):
            return TermCheck(term, True, "The list is full: this term would be left out until you remove others "
                                         "(Settings → Vocabulary).", "warn", fits=False)
    return TermCheck(term, True, "", "ok")


def add_term(existing: Iterable[str], raw: str) -> list[str] | None:
    """The vocabulary with ``raw`` added at the end, or None when ``check_new_term`` refuses it (duplicates
    and invalid terms are not added)."""
    have = list(existing)
    check = check_new_term(raw, have)
    return [*have, check.term] if check.ok else None


# -- sharing the list as a text file (L38) ----------------------------------------------------------------
MAX_FILE_BYTES = 1_000_000  # a vocabulary is a few hundred bytes; refuse something that is clearly not one


class VocabFileError(Exception):
    """The file could not be used; ``str(exc)`` is fit to show the user."""


def read_terms_file(path: Path | str) -> list[str]:
    """Terms from a text file, one per line. UTF-8 with or without a BOM (a file in the ANSI code page is read as
    cp1252), CRLF or LF; blank lines and ``#`` comment lines are ignored and duplicates (ignoring case) dropped,
    as in the Settings box."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise VocabFileError(f"That file is {size // 1024} KB; a vocabulary file is much smaller than "
                                 f"{MAX_FILE_BYTES // 1024} KB.")
        data = path.read_bytes()
    except OSError as exc:
        raise VocabFileError(f"Could not read the file: {exc.strerror or exc}") from exc
    if b"\x00" in data:
        raise VocabFileError("That does not look like a text file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return parse_terms(text)


def write_terms_file(path: Path | str, terms: Iterable[str]) -> int:
    """Write the terms as UTF-8, one per line, LF line ends, with a final newline. Returns how many were written."""
    items = parse_terms("\n".join(t for t in terms if t))
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{t}\n" for t in items))
    except OSError as exc:
        raise VocabFileError(f"Could not save the file: {exc.strerror or exc}") from exc
    return len(items)


@dataclass(frozen=True)
class ImportResult:
    terms: list[str]  # the list after the import
    added: int  # terms that were not in the list before
    duplicates: int  # file terms already in the list (ignored when merging)
    replaced: bool

    @property
    def message(self) -> str:
        if self.replaced:
            return f"Replaced the list with {len(self.terms)} term{'' if len(self.terms) == 1 else 's'} from the file."
        extra = f" {self.duplicates} already in the list." if self.duplicates else ""
        return f"Added {self.added} term{'' if self.added == 1 else 's'} from the file.{extra}"


def keep_added(box: Iterable[str], opened_with: Iterable[str], now: Iterable[str]) -> list[str]:
    """The list to save from a dialog's box that was filled with ``opened_with``: the box as edited, plus the terms
    added elsewhere since (in ``now`` but not in ``opened_with``, such as "Add to vocabulary…" from a line), skipping
    any already in the box (ignoring case). A term deleted in the box stays deleted."""
    before = {t.casefold() for t in opened_with}
    return merge_import(box, [t for t in now if t.casefold() not in before]).terms


def merge_import(current: Iterable[str], imported: Iterable[str], *, replace: bool = False) -> ImportResult:
    """Combine the list with imported terms: ``replace`` keeps only the imported ones; otherwise the imported ones
    are appended to the current list, skipping any already there (ignoring case)."""
    imported = list(imported)
    if replace:
        return ImportResult(imported, len(imported), 0, True)
    have = list(current)
    seen = {t.casefold() for t in have}
    new = [t for t in imported if t.casefold() not in seen]
    return ImportResult([*have, *new], len(new), len(imported) - len(new), False)
