"""tools/wer.py: normalisation and word error rate, on synthetic text only."""

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import wer  # noqa: E402


# -- normalisation ------------------------------------------------------------------------------
@pytest.mark.parametrize("text, words", [
    ("Hello, World!", ["hello", "world"]),
    ("  The   quick\nbrown fox.  ", ["the", "quick", "brown", "fox"]),
    ("Don't stop; it’s fine.", ["dont", "stop", "its", "fine"]),
    ("a well-known end-to-end test", ["a", "well", "known", "end", "to", "end", "test"]),
    ("Okay, alright then", ["ok", "all", "right", "then"]),
    ("R&D", ["r", "and", "d"]),
    ("", []),
    ("...", []),
])
def test_normalize_case_and_punctuation(text, words):
    assert wer.normalize(text) == words


@pytest.mark.parametrize("text, words", [
    ("0", "zero"),
    ("7 apples", "seven apples"),
    ("13", "thirteen"),
    ("20", "twenty"),
    ("42", "forty two"),
    ("100", "one hundred"),
    ("305", "three hundred five"),
    ("1,250", "one thousand two hundred fifty"),
    ("2026", "two thousand twenty six"),
    ("1000000", "one million"),
    ("3.5", "three point five"),
    ("0.25", "zero point two five"),
    ("50%", "fifty percent"),
    ("$5", "five dollars"),
    ("$1", "one dollar"),
    ("1st 2nd 3rd 4th", "first second third fourth"),
    ("the 21st and the 30th", "the twenty first and the thirtieth"),
    ("12th", "twelfth"),
    ("007", "zero zero seven"),
    ("5551234567", "five five five one two three four five six seven"),
    ("at 3:30", "at three thirty"),
    ("version v2 and b2b", "version v2 and b2b"),  # digits inside a word are left alone
    ("Room 12.", "room twelve"),
])
def test_normalize_numbers(text, words):
    assert wer.normalize(text) == words.split()


def test_number_words_matches_spelled_out_text():
    assert wer.normalize("twenty-two") == wer.normalize("22")
    assert wer.normalize("Three hundred and five") != wer.normalize("305")  # "and" is not added


# -- scoring ------------------------------------------------------------------------------------
def test_identical_text_scores_zero():
    s = wer.wer("The cat sat on the mat.", "the cat sat, on the mat")
    assert (s.substitutions, s.deletions, s.insertions) == (0, 0, 0)
    assert s.wer == 0.0
    assert s.ref_words == s.hyp_words == 6


def test_one_of_each_error():
    ref = "one two three four five six"
    assert wer.wer(ref, "one two tree four five six").substitutions == 1
    assert wer.wer(ref, "one two four five six").deletions == 1
    assert wer.wer(ref, "one two three three four five six").insertions == 1
    s = wer.wer("alpha beta gamma delta", "alpha bravo gamma delta echo")
    assert (s.substitutions, s.deletions, s.insertions) == (1, 0, 1)
    assert s.wer == pytest.approx(0.5)


def test_wer_divides_by_reference_words_and_can_pass_one():
    s = wer.wer("yes", "oh yes indeed it is")
    assert s.errors == 4 and s.wer == pytest.approx(4.0)


def test_empty_sides():
    assert wer.wer("", "").wer == 0.0
    assert wer.wer("", "word").wer == float("inf")
    s = wer.wer("three little words", "")
    assert (s.deletions, s.wer) == (3, 1.0)


def test_numbers_do_not_count_as_errors_when_written_differently():
    assert wer.wer("We shipped 22 boxes on the 3rd.", "we shipped twenty-two boxes on the third").errors == 0


def _slow_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def test_distance_and_alignment_agree_with_a_plain_implementation():
    rng = random.Random(7)
    vocab = ["red", "green", "blue", "cyan", "pink"]
    for _ in range(200):
        a = [rng.choice(vocab) for _ in range(rng.randrange(0, 12))]
        b = [rng.choice(vocab) for _ in range(rng.randrange(0, 12))]
        want = _slow_distance(a, b)
        assert wer.distance(a, b) == want
        ops = wer.align(a, b)
        assert sum(1 for op, _r, _h in ops if op != "ok") == want
        assert [r for _op, r, _h in ops if r is not None] == a
        assert [h for _op, _r, h in ops if h is not None] == b
        assert all(r == h for op, r, h in ops if op == "ok")


# -- files --------------------------------------------------------------------------------------
def _jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


ROWS = [
    {"type": "meeting_start", "meeting_id": "m"},
    {"type": "segment", "seq": 1, "source": "system", "start_ms": 1000, "end_ms": 2000, "text": "Good morning all."},
    {"type": "segment", "seq": 2, "source": "mic", "start_ms": 2500, "end_ms": 3000, "text": "Morning."},
    {"type": "segment", "seq": 3, "source": "system", "start_ms": 9000, "end_ms": 9900, "text": "Item 2 is next."},
    {"type": "meeting_end"},
]


def test_jsonl_transcript_against_reference(tmp_path):
    hyp = _jsonl(tmp_path / "2030-01-01_0900_sample" / "transcript.jsonl", ROWS)
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    (ref_dir / "2030-01-01_0900_sample.txt").write_text(
        "# corrected by ear\nGood morning, all.\nMorning!\nItem two is next.\n", encoding="utf-8")
    assert wer.meeting_of(hyp) == "2030-01-01_0900_sample"
    s, _ops = wer.score_file(hyp, ref_dir=ref_dir)
    assert s.errors == 0 and s.ref_words == 8


def test_lines_are_scored_in_time_order_and_by_source(tmp_path):
    rows = [ROWS[3], ROWS[1], ROWS[2]]
    assert wer.hyp_text(rows) == "Good morning all. Morning. Item 2 is next."
    assert wer.hyp_text(rows, source="mic") == "Morning."
    assert wer.hyp_text(rows, until_s=5) == "Good morning all. Morning."
    assert wer.hyp_text(rows, from_s=2, until_s=5) == "Morning."


def test_reference_can_cover_only_part_of_the_meeting(tmp_path):
    hyp = _jsonl(tmp_path / "meet" / "transcript.final.jsonl", ROWS)
    ref = tmp_path / "part.txt"
    ref.write_text("# until_s: 5\nGood morning all. Morning.\n", encoding="utf-8")
    s, _ops = wer.score_file(hyp, ref)
    assert (s.errors, s.ref_words, s.hyp_words) == (0, 4, 4)
    s, _ops = wer.score_file(hyp, ref, until_s=60)  # the option overrides the file
    assert (s.insertions, s.hyp_words) == (4, 8)


def test_retx_json_and_missing_reference(tmp_path, capsys):
    run = tmp_path / "retx" / "2030-01-01_0900_sample__x1.json"
    run.parent.mkdir()
    run.write_text(json.dumps({"tag": "x1", "rows": [r for r in ROWS if r.get("type") == "segment"]}),
                   encoding="utf-8")
    assert wer.meeting_of(run) == "2030-01-01_0900_sample"
    assert wer.score_file(run, ref_dir=tmp_path / "ref") is None
    assert wer.main([str(run), "--ref-dir", str(tmp_path / "ref")]) == 1
    assert "no reference transcript" in capsys.readouterr().out
    (tmp_path / "ref").mkdir()
    (tmp_path / "ref" / "2030-01-01_0900_sample.txt").write_text(
        "Good morning all. Morning. Item two was next.", encoding="utf-8")
    assert wer.main([str(run), "--ref-dir", str(tmp_path / "ref"), "--show", "3"]) == 0
    out = capsys.readouterr().out
    assert "WER 12.5%" in out and "was -> is" in out
