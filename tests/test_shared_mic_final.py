"""Shared mic in the final pass (#54): only mic pieces that start inside a stretch are separated.
Fakes and hand-written meeting folders only."""

import numpy as np
import pytest
import soundfile as sf

from huddle_helper import finalize, meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.diarize import Turn
from huddle_helper.finalize import append_words, diarize_stretches, finalize_meeting
from huddle_helper.shared_mic import SharedMic
from huddle_helper.segments import Segment, Word
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event, read_events

from .conftest import ToneEmbedder, tone

LENGTH_MS = 20_000


class RecordingDiarizer:
    """Fake split-point step: records how much audio it was given and returns one turn over all of it."""

    calls: list[int] = []

    def __init__(self):
        RecordingDiarizer.calls = []

    def diarize(self, audio, progress=None):
        RecordingDiarizer.calls.append(len(audio))
        if progress:
            progress(1.0)
        return [Turn(0, len(audio) * 1000 // SAMPLE_RATE, 0)] if len(audio) else []


class AmpEmbedder:
    """The voice of a clip is read from its level: theta = mean * 10 (as in test_tags)."""

    def embed(self, audio):
        theta = float(np.mean(audio)) * 10
        return np.array([np.cos(theta), np.sin(theta), 0.0], np.float32)


# (start_ms, end_ms, text, live speaker, voice): a voice is a tone frequency (ToneEmbedder) or,
# with ``level=True``, a constant level (AmpEmbedder).
LINES = [
    (1000, 2000, "Before.", "You", 300),
    (4500, 5500, "Alpha.", "MIC_SPEAKER_01", 300),
    (6000, 7000, "Bravo.", "MIC_SPEAKER_02", 1300),
    (7500, 8500, "Across.", "MIC_SPEAKER_01", 300),  # starts inside the first stretch, ends after it
    (9000, 10000, "Between.", "You", 300),
    (11500, 12500, "Into.", "You", 1300),  # starts before the second stretch, ends inside it
    (12600, 13500, "Again.", "MIC_SPEAKER_01", 300),
    (14000, 15000, "Later.", "MIC_SPEAKER_02", 1300),
]
STRETCHES = [[4000, 8000], [12000, 16000]]


def build(tmp_path, lines=LINES, *, shared_mic=None, diarize_mic=True, tags=None, level=False, name="Fx"):
    meta = {"settings": {"diarize_system": False, "diarize_mic": diarize_mic, "keep_audio": False}}
    if shared_mic is not None:
        meta["shared_mic"] = shared_mic
    started = mt.now_local().replace(year=2026, month=1, day=15, hour=9, minute=30, second=0, microsecond=0)
    folder = mt.MeetingFolder.create(tmp_path, name, started, meta)
    audio = np.zeros(LENGTH_MS * SAMPLE_RATE // 1000, np.float32)
    segs = []
    for start, end, text, speaker, voice in lines:
        a, b = start * SAMPLE_RATE // 1000, end * SAMPLE_RATE // 1000
        audio[a:b] = voice / 10 if level else tone(voice, (end - start) / 1000)[:b - a]
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


def final(folder, embedder=ToneEmbedder):
    RecordingDiarizer.calls = []  # also when the pass never makes a diarizer
    finalize_meeting(folder, RecordingDiarizer, default_keep_audio=False, embedder_factory=embedder)
    return [(e["start_ms"], e["text"], e["speaker"]) for e in read_events(folder.final_path)
            if e["type"] == "segment"]


def test_only_pieces_starting_inside_a_stretch_are_separated(tmp_path):
    got = final(build(tmp_path, shared_mic=STRETCHES))
    assert [(start, sp) for start, _text, sp in got] == [
        (1000, "You"), (4500, "MIC_SPEAKER_01"), (6000, "MIC_SPEAKER_02"),
        (7500, "MIC_SPEAKER_01"),  # crosses the end of a stretch: the mode at its start
        (9000, "You"),
        (11500, "You"),  # crosses the start of a stretch: the mode at its start
        (12600, "MIC_SPEAKER_01"),  # same voice in the second stretch: same speaker
        (14000, "MIC_SPEAKER_02")]


def test_split_point_step_gets_only_stretch_audio_one_stretch_at_a_time(tmp_path):
    folder = build(tmp_path, shared_mic=STRETCHES)
    final(folder)
    assert RecordingDiarizer.calls == [4000 * SAMPLE_RATE // 1000, 4000 * SAMPLE_RATE // 1000]
    meta = folder.read_meta()
    assert "diarize_mic" in meta["final_stats"]["stages"]  # the mic's separation time is still reported
    assert meta["refined_streams"] == ["mic"]


def test_no_stretch_reads_no_mic_audio_for_separation(tmp_path, monkeypatch):
    read = []
    real = finalize.read_audio
    monkeypatch.setattr(finalize, "read_audio", lambda path: read.append(path) or real(path))
    folder = build(tmp_path, shared_mic=[], diarize_mic=False)
    got = final(folder)
    assert read == [] and RecordingDiarizer.calls == []
    assert {sp for _s, _t, sp in got} == {"You"}
    assert "mic" not in folder.read_meta()["refined_streams"]


def test_whole_meeting_stretch_and_old_format_meeting_match(tmp_path):
    old = build(tmp_path / "old", shared_mic=None, diarize_mic=True)  # no list: one whole-meeting stretch
    assert "shared_mic" not in old.read_meta()
    old_final = final(old)
    old_calls = RecordingDiarizer.calls
    whole = build(tmp_path / "whole", shared_mic=[[0, LENGTH_MS]])
    whole_final = final(whole)
    assert old_calls == RecordingDiarizer.calls == [LENGTH_MS * SAMPLE_RATE // 1000]  # the whole stream, once
    assert old_final == whole_final
    assert all(sp.startswith("MIC_SPEAKER_") for _s, _t, sp in whole_final)  # every mic line separated


def test_whole_meeting_stretch_gives_the_same_pieces_as_no_stretch_list():
    segs = [Segment("mic", s, e, t, 0.9, sp, [Word(s, e, " " + t)]) for s, e, t, sp, _v in LINES]
    words = {("mic", s.start_ms): s.words for s in segs}
    turns = {"mic": [Turn(0, 9000, 0), Turn(9000, LENGTH_MS, 1)]}

    def run(stretches):
        fresh = [Segment(s.source, s.start_ms, s.end_ms, s.text, s.confidence, s.speaker, list(s.words))
                 for s in segs]
        return [(s.start_ms, s.speaker) for s in finalize.relabel(fresh, words, turns, {"mic"},
                                                                  mic_stretches=stretches)]

    assert run(None) == run(SharedMic(((0, None),))) == run(SharedMic(((0, LENGTH_MS),)))


# -- names given to live mic speakers -------------------------------------------------------------
NAMED = [  # levels: A 0.0 and B 0.45 are 0.9 alike (merged without names); C 0.05 is A's twin
    (4500, 5500, "Alpha.", "MIC_SPEAKER_01", 0.0),
    (6000, 7000, "Bravo.", "MIC_SPEAKER_02", 0.45),
    (12600, 13500, "Again.", "MIC_SPEAKER_01", 0.05),
]


def ln(start, end, name):
    return {"type": "tag", "at": "t", "scope": "line", "source": "mic", "start_ms": start, "end_ms": end,
            "name": name}


def test_names_keep_differently_named_mic_speakers_apart_and_same_named_together(tmp_path):
    plain = final(build(tmp_path / "plain", NAMED, shared_mic=STRETCHES, level=True), AmpEmbedder)
    assert {sp for _s, _t, sp in plain} == {"MIC_SPEAKER_01"}  # no names: all one voice

    tags = [ln(4500, 5500, "Ana"), ln(6000, 7000, "Ben"), ln(12600, 13500, "Ana")]
    folder = build(tmp_path / "named", NAMED, shared_mic=STRETCHES, level=True, tags=tags)
    got = [sp for _s, _t, sp in final(folder, AmpEmbedder)]
    assert got[0] == got[2] != got[1]
    names = folder.read_speakers()
    assert {names[got[0]], names[got[1]]} == {"Ana", "Ben"}


# -- the pieces of the step -----------------------------------------------------------------------
def test_diarize_stretches_puts_turns_on_the_meeting_clock_with_separate_ids():
    seen = []
    turns = diarize_stretches(RecordingDiarizer(), np.zeros(10 * SAMPLE_RATE, np.float32),
                              SharedMic(((6000, 8000), (1000, 3000))), seen.append)
    assert [(t.start_ms, t.end_ms) for t in turns] == [(1000, 3000), (6000, 8000)]
    assert turns[0].speaker != turns[1].speaker
    assert seen == sorted(seen) and seen[-1] == 1.0
