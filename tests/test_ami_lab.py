"""tools/ami_lab.py: reading AMI annotations and scoring speakers, on made-up data only."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ami_lab  # noqa: E402

WORDS = """<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>
<nite:root nite:id="XX0001a.A.words" xmlns:nite="http://nite.sourceforge.net/">
   <w nite:id="w0" starttime="1.0" endtime="1.2">Hello</w>
   <w nite:id="w1" starttime="1.2" endtime="1.2" punc="true">,</w>
   <w nite:id="w2" starttime="1.25" endtime="1.5" trunc="true">wo</w>
   <vocalsound nite:id="w3" starttime="1.5" endtime="1.6" type="cough"/>
   <w nite:id="w4" starttime="1.6" endtime="1.9">world</w>
   <w nite:id="w5" starttime="4.0" endtime="4.3">again</w>
</nite:root>"""
MEETINGS = """<?xml version="1.0"?>
<nite:root xmlns:nite="http://nite.sourceforge.net/">
   <meeting observation="XX0001a">
      <speaker nxt_agent="B" channel="1" global_name="FEE001"/>
      <speaker nxt_agent="A" channel="0" global_name="MEE002"/>
   </meeting>
</nite:root>"""


@pytest.fixture
def annotations():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("words/XX0001a.A.words.xml", WORDS)
        z.writestr("corpusResources/meetings.xml", MEETINGS)
    return zipfile.ZipFile(buf)


def test_words_leave_out_punctuation_noises_and_cut_off_words(annotations):
    assert ami_lab.words(annotations, "XX0001a", "A") == [(1000, 1200, "Hello"), (1600, 1900, "world"),
                                                          (4000, 4300, "again")]


def test_speakers_map_letters_to_headsets(annotations):
    assert ami_lab.speakers(annotations, "XX0001a") == {"B": {"channel": 1, "id": "FEE001"},
                                                        "A": {"channel": 0, "id": "MEE002"}}
    with pytest.raises(SystemExit):
        ami_lab.speakers(annotations, "XX0009z")


def test_turns_join_words_closer_than_the_gap(annotations):
    assert ami_lab.turns(ami_lab.words(annotations, "XX0001a", "A")) == [[1000, 1900], [4000, 4300]]


def test_word_errors_with_and_without_fillers():
    with_fillers, _ = ami_lab.word_errors("um so we mm-hmm start now", "so we start now")
    assert (with_fillers.ref_words, with_fillers.deletions, with_fillers.errors) == (7, 3, 3)
    without, _ = ami_lab.word_errors("um so we mm-hmm start now", "so we start now", fillers=False)
    assert (without.ref_words, without.errors) == (4, 0)


def test_speaker_table_purity_and_coverage():
    truth = {"turns": [[0, 10_000, "B"], [10_000, 20_000, "C"]]}
    lines = [{"start_ms": 0, "end_ms": 10_000, "speaker": "Speaker 1"},  # B
             {"start_ms": 10_000, "end_ms": 15_000, "speaker": "Speaker 1"},  # C, under B's label
             {"start_ms": 15_000, "end_ms": 20_000, "speaker": "Speaker 2"},  # C
             {"start_ms": 30_000, "end_ms": 35_000, "speaker": "Speaker 2"}]  # nobody talking
    table, purity, coverage, silent = ami_lab.speaker_table(lines, truth)
    assert dict(table["Speaker 1"]) == {"B": 10.0, "C": 5.0}
    assert dict(table["Speaker 2"]) == {"C": 5.0, "-": 5.0}
    assert purity == pytest.approx(15 / 20)
    assert coverage == pytest.approx(15 / 20)
    assert silent == pytest.approx(5 / 25)


def test_gate_silences_audio_outside_the_spans():
    import numpy as np

    audio = np.ones(3 * ami_lab.RATE, np.float32)
    out = ami_lab.gate(audio, [[1000, 2000]], pad_ms=100)
    assert out[: ami_lab.RATE // 2].max() == 0.0 and out[-ami_lab.RATE // 2:].max() == 0.0
    assert out[ami_lab.RATE:2 * ami_lab.RATE].min() == pytest.approx(1.0)
    assert len(out) == len(audio)


def test_meeting_in_a_run_folder_name():
    assert ami_lab.meeting_in("2026-11-11_0312_ami-is1009a") == "IS1009a"
    assert ami_lab.meeting_in("2026-11-11_0312_standup") is None
