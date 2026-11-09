import json

import numpy as np

from huddle_helper import meeting as mt
from huddle_helper.diarize import OnlineClusterer, Turn
from huddle_helper.finalize import append_words, finalize_meeting, load_words, relabel, speaker_at
from huddle_helper.segments import Segment, Word
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event, read_events


def unit(i, n=4):
    v = np.zeros(n, np.float32)
    v[i] = 1
    return v


def test_online_clusterer_groups_and_separates():
    c = OnlineClusterer("SPEAKER_", threshold=0.6, min_new_speaker_s=1.0)
    assert c.assign(unit(0), 2.0) == "SPEAKER_01"
    assert c.assign(unit(1), 2.0) == "SPEAKER_02"
    assert c.assign(unit(0) * 0.9 + unit(2) * 0.1, 2.0) == "SPEAKER_01"
    # A short clip of an unknown voice is attached to the closest speaker, not a new one.
    assert c.assign(unit(3), 0.5) in ("SPEAKER_01", "SPEAKER_02")
    assert c.count == 2
    assert c.assign(None, 1.0) in ("SPEAKER_01", "SPEAKER_02")


def test_online_clusterer_starts_without_fingerprint():
    c = OnlineClusterer("SPEAKER_")
    assert c.assign(None, 0.3) == "SPEAKER_01"
    assert c.assign(unit(0), 2.0) == "SPEAKER_01"
    assert c.assign(unit(1), 2.0) == "SPEAKER_02"


def test_speaker_at_overlap_and_nearest():
    turns = [Turn(0, 1000, 0), Turn(1000, 3000, 1)]
    assert speaker_at(800, 2000, turns) == 1
    assert speaker_at(3500, 3600, turns) == 1  # nearest within 2 s
    assert speaker_at(9000, 9100, turns) is None


def test_relabel_splits_segment_at_speaker_change():
    words = [Word(0, 300, " Sounds"), Word(300, 600, " good."), Word(1200, 1500, " I"),
             Word(1500, 1800, " disagree"), Word(1800, 2100, " though.")]
    seg = Segment("system", 0, 2100, "Sounds good. I disagree though.", 0.8, "SPEAKER_01", words)
    turns = [Turn(0, 900, 7), Turn(1100, 2200, 3)]
    out = relabel([seg], {("system", 0): words}, {"system": turns}, {"system"})
    assert [(s.speaker, s.text) for s in out] == [("SPEAKER_01", "Sounds good."),
                                                  ("SPEAKER_02", "I disagree though.")]
    assert out[1].start_ms == 1200 and out[1].end_ms == 2100


def test_relabel_undiarized_and_failed_streams():
    segs = [Segment("mic", 0, 1000, "hi", 0.9, "You"), Segment("system", 0, 1000, "yo", 0.9, "SPEAKER_04")]
    out = relabel(segs, {}, {"system": None}, {"system"})
    assert [s.speaker for s in out] == ["You", "SPEAKER_04"]  # failed pass keeps live labels
    out = relabel(segs, {}, {}, set())
    assert [s.speaker for s in out] == ["You", "Remote"]


class FakeDiarizer:
    def diarize(self, audio, progress=None):
        if progress:
            progress(1.0)
        return [Turn(0, 2000, 5), Turn(2000, 9000, 2)]


def _make_meeting(tmp_path, keep_audio=False):
    started = mt.now_local()
    folder = mt.MeetingFolder.create(tmp_path, "Test", started, {
        "settings": {"diarize_system": True, "diarize_mic": False, "keep_audio": keep_audio}})
    import soundfile as sf
    sf.write(str(folder.audio_file("system")), np.zeros(16000 * 3, np.float32), 16000, format="FLAC")
    segs = [Segment("mic", 100, 900, "hello", 0.9, "You"),
            Segment("system", 500, 1500, "first", 0.9, "SPEAKER_01", [Word(500, 1500, " first")]),
            Segment("system", 2500, 3000, "second", 0.9, "SPEAKER_01", [Word(2500, 3000, " second")])]
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, mt.iso(started)))
        for i, s in enumerate(segs, 1):
            w.write(s.to_event(i))
            append_words(folder.words_path, s)
        w.write(meeting_end_event(mt.iso(started), len(segs)))
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)
    return folder


def test_finalize_writes_final_then_done_and_removes_audio(tmp_path):
    folder = _make_meeting(tmp_path)
    assert load_words(folder.words_path)[("system", 2500)][0].text == " second"
    count = finalize_meeting(folder, FakeDiarizer, keep_audio=False)
    assert count == 3
    events = read_events(folder.final_path)
    assert events[0]["type"] == "meeting_start" and events[-1]["type"] == "meeting_end"
    assert [e["speaker"] for e in events[1:-1]] == ["You", "SPEAKER_01", "SPEAKER_02"]
    assert [e["seq"] for e in events[1:-1]] == [1, 2, 3]
    assert folder.is_done and not folder.audio_dir.exists() and not folder.final_tmp_path.exists()
    meta = folder.read_meta()
    assert meta["status"] == "complete" and meta["refined_streams"] == ["system"]


def test_finalize_keeps_audio_when_asked(tmp_path):
    folder = _make_meeting(tmp_path, keep_audio=True)
    finalize_meeting(folder, FakeDiarizer, keep_audio=True)
    assert folder.audio_file("system").exists() and folder.read_meta()["audio_kept"] is True


def test_finalize_survives_diarizer_failure(tmp_path):
    def broken():
        raise RuntimeError("model missing")

    folder = _make_meeting(tmp_path)
    finalize_meeting(folder, broken, keep_audio=False)
    speakers = [e["speaker"] for e in read_events(folder.final_path) if e["type"] == "segment"]
    assert speakers == ["You", "SPEAKER_01", "SPEAKER_01"]
    assert folder.is_done


def test_speakers_json(tmp_path):
    folder = _make_meeting(tmp_path)
    folder.write_speakers({"SPEAKER_01": " Dana ", "SPEAKER_02": "", "You": "You"})
    data = json.loads(folder.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"] == {"SPEAKER_01": "Dana"}
    assert folder.read_speakers() == {"SPEAKER_01": "Dana"}
