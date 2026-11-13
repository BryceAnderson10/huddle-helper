"""The final pass recognises You inside a shared mic stretch (#56): only with enough mic speech outside
the stretches, a very confident match and a clear lead. Fake fingerprints and hand-written meetings only."""

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.diarize import PROFILES, profile
from huddle_helper.finalize import Voices, append_words, finalize_meeting, you_decision
from huddle_helper.segments import Segment, Word
from huddle_helper.transcript_model import TranscriptModel
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event, read_events

from .test_shared_mic_final import AmpEmbedder, RecordingDiarizer

LENGTH_MS = 40_000
STRETCH = [[10_000, 29_000]]
YOU = 0.2  # a voice is an angle (AmpEmbedder: theta = level * 10); the user's


def lines(a, b):
    """Off-stretch speech by the user (16 s) and two mic speakers inside the stretch at angles a and b."""
    return [(0, 4000, "One.", "You", YOU), (5000, 9000, "Two.", "You", YOU),
            (11_000, 14_000, "Alpha.", "MIC_SPEAKER_01", a), (15_000, 18_000, "Bravo.", "MIC_SPEAKER_02", b),
            (19_000, 22_000, "Charlie.", "MIC_SPEAKER_01", a), (23_000, 26_000, "Delta.", "MIC_SPEAKER_02", b),
            (30_000, 34_000, "Three.", "You", YOU), (35_000, 39_000, "Four.", "You", YOU)]


MATCH = lines(YOU, YOU + 1.8)  # the user plus someone else: 1.0 and cos(1.8) < 0 alike to the user


def build(tmp_path, ls=MATCH, shared_mic=STRETCH, tags=None):
    meta = {"settings": {"diarize_system": False, "diarize_mic": True, "keep_audio": False},
            "shared_mic": shared_mic}
    started = mt.now_local().replace(year=2026, month=1, day=15, hour=9, minute=30, second=0, microsecond=0)
    folder = mt.MeetingFolder.create(tmp_path, "Fx", started, meta)
    audio = np.zeros(LENGTH_MS * SAMPLE_RATE // 1000, np.float32)
    segs = []
    for start, end, text, speaker, theta in ls:
        audio[start * SAMPLE_RATE // 1000:end * SAMPLE_RATE // 1000] = theta / 10
        segs.append(Segment("mic", start, end, text, 0.9, speaker, [Word(start, end, " " + text)]))
    sf.write(str(folder.audio_file("mic")), audio, SAMPLE_RATE, format="FLAC")
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, mt.iso(started)))
        for i, s in enumerate(segs, 1):
            w.write(s.to_event(i))
            append_words(folder.words_path, s)
        w.write(meeting_end_event(mt.iso(started), len(segs)))
    for e in tags or []:
        mt.append_jsonl(folder.tags_path, e)
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)
    return folder


ENOUGH = profile(you_min_s=10.0)  # the shipped cutoffs, with a reference short enough for a test


def final(folder, voice=ENOUGH):
    finalize_meeting(folder, RecordingDiarizer, default_keep_audio=False, embedder_factory=AmpEmbedder, voice=voice)
    return {e["text"]: e["speaker"] for e in read_events(folder.final_path) if e["type"] == "segment"}


OFF = {"One.": "You", "Two.": "You", "Three.": "You", "Four.": "You"}


def test_the_matching_mic_speaker_becomes_you_and_the_other_stays(tmp_path):
    folder = build(tmp_path)
    assert final(folder) == {**OFF, "Alpha.": "You", "Charlie.": "You",
                             "Bravo.": "MIC_SPEAKER_01", "Delta.": "MIC_SPEAKER_01"}
    diag = folder.read_meta()["you_match"]
    assert diag["matched"] is True and diag["reason"] == "matched"
    assert diag["similarity"] == pytest.approx(1.0, abs=1e-3) and diag["runner_up"] < 0
    assert diag["reference_s"] == 16.0


def test_too_little_off_stretch_speech_relabels_nothing(tmp_path):
    folder = build(tmp_path)
    got = final(folder, profile(you_min_s=20.0))
    assert got["Alpha."].startswith("MIC_SPEAKER_") and got["Bravo."].startswith("MIC_SPEAKER_")
    assert got["Alpha."] != got["Bravo."]
    diag = folder.read_meta()["you_match"]
    assert (diag["matched"], diag["reason"], diag["reference_s"]) == (False, "too_little_reference", 16.0)
    assert diag["similarity"] == pytest.approx(1.0, abs=1e-3)  # recorded even when not used


def test_two_mic_speakers_both_close_to_the_user_relabel_nothing(tmp_path):
    # each cos(0.6) = 0.83 like the user, but only cos(1.2) = 0.36 like each other: two speakers
    folder = build(tmp_path, lines(YOU + 0.6, YOU - 0.6))
    got = final(folder)
    assert {got["Alpha."], got["Bravo."]} == {"MIC_SPEAKER_01", "MIC_SPEAKER_02"}
    diag = folder.read_meta()["you_match"]
    assert (diag["matched"], diag["reason"]) == (False, "no_clear_lead")
    assert diag["similarity"] == pytest.approx(diag["runner_up"], abs=1e-3)


def test_best_match_below_the_cutoff_relabels_nothing(tmp_path):
    folder = build(tmp_path, lines(YOU + 1.0, YOU + 2.5))  # cos(1.0) = 0.54, under the shipped 0.65
    got = final(folder)
    assert "You" not in {got["Alpha."], got["Bravo."]}
    diag = folder.read_meta()["you_match"]
    assert (diag["matched"], diag["reason"]) == (False, "below_cutoff")
    assert diag["similarity"] == pytest.approx(np.cos(1.0), abs=1e-3)


def test_whole_meeting_shared_mic_has_no_reference_and_relabels_nothing(tmp_path):
    folder = build(tmp_path, shared_mic=[[0, LENGTH_MS]])
    got = final(folder)
    assert all(sp.startswith("MIC_SPEAKER_") for sp in got.values())
    diag = folder.read_meta()["you_match"]
    assert (diag["matched"], diag["reason"], diag["reference_s"]) == (False, "no_reference", 0.0)


def test_a_meeting_without_shared_mic_records_no_decision(tmp_path):
    folder = build(tmp_path, shared_mic=[])
    assert set(final(folder).values()) == {"You"}
    assert "you_match" not in folder.read_meta()


def line_tag(start, end, name):
    return {"type": "tag", "at": "t", "scope": "line", "source": "mic", "start_ms": start, "end_ms": end,
            "name": name}


def test_a_mic_speaker_the_user_named_keeps_that_name(tmp_path):
    folder = build(tmp_path, tags=[line_tag(11_000, 14_000, "Ana")])
    got = final(folder)
    assert got["Alpha."] == got["Charlie."] != "You"
    assert folder.read_speakers()[got["Alpha."]] == "Ana"
    assert folder.read_meta()["you_match"]["reason"] == "named"


def test_a_name_for_you_applies_to_the_relabelled_speaker_in_the_transcript_window(tmp_path):
    you_name = {"type": "tag", "at": "t", "scope": "speaker", "label": "You", "name": "Me"}
    folder = build(tmp_path, tags=[you_name])
    got = final(folder)
    assert got["Alpha."] == got["Charlie."] == "You"
    speakers = folder.read_speakers()
    assert speakers == {"You": "Me"}  # one entry: the transcript and speakers.json agree
    rows = {r.text: r for r in TranscriptModel.load(folder).lines}
    assert (rows["Alpha."].label, rows["Alpha."].display) == ("You", "Me")
    assert rows["Alpha."].person_key == rows["One."].person_key  # the same person in the window
    assert rows["Bravo."].display == "MIC_SPEAKER_01"


def test_the_relabelled_speaker_shows_as_you_without_names(tmp_path):
    folder = build(tmp_path)
    final(folder)
    rows = {r.text: r for r in TranscriptModel.load(folder).lines}
    assert rows["Alpha."].display == rows["One."].display == "You"


# -- the decision on its own --------------------------------------------------------------------------
def unit(theta):
    return np.array([np.cos(theta), np.sin(theta), 0.0], np.float32)


def voices(*clusters, names=None):
    """Voices from (theta, seconds, cluster) triples."""
    return Voices([unit(t) for t, _s, _c in clusters], [s for _t, s, _c in clusters],
                  list(range(len(clusters))), [c for _t, _s, c in clusters], names or [None] * len(clusters))


VOICE = profile()


def test_decision_cutoffs():
    ref = [unit(0.0)] * 3
    w = [12.0] * 3  # 36 s, over the shipped minimum
    clear = voices((0.05, 5, 0), (0.0, 5, 0), (2.0, 5, 1))
    assert you_decision(ref, w, clear, VOICE)["cluster"] == 0
    assert you_decision(ref, [1.0] * 3, clear, VOICE)["reason"] == "too_little_reference"
    close = voices((0.5, 5, 0), (-0.5, 5, 1))
    assert you_decision(ref, w, close, VOICE)["reason"] == "no_clear_lead"
    far = voices((1.2, 5, 0))
    assert you_decision(ref, w, far, VOICE)["reason"] == "below_cutoff"
    one = voices((0.1, 5, 0))  # a single mic speaker has no runner-up to lead
    d = you_decision(ref, w, one, VOICE)
    assert d["matched"] and d["runner_up"] is None
    assert you_decision([], [], one, VOICE)["reason"] == "no_reference"
    assert you_decision(ref, w, None, VOICE)["reason"] == "no_mic_speakers"
    assert you_decision(ref, w, voices((0.1, 5, 0), names=["Ana"]), VOICE)["reason"] == "named"
    assert you_decision(ref, w, one, VOICE, named={0})["reason"] == "named"


def test_cutoffs_are_conservative_for_every_model():
    for p in PROFILES.values():
        assert p.you_match >= p.final_match + 0.15  # well above the cutoff used to merge speakers
        assert p.you_lead >= 0.10 and p.you_min_s >= 30.0
