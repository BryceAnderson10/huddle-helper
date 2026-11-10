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


def test_online_clusterer_short_opening_line_is_adopted():
    # Test #3: a 1.0 s opening line founded a speaker, then the same voice's 5.8 s line
    # (0.70 alike, under the 0.75 threshold) became a second speaker.
    c = OnlineClusterer("SPEAKER_")
    first = unit(0)
    same_voice = 0.7 * unit(0) + np.sqrt(1 - 0.49) * unit(1)
    assert c.assign(first, 1.0) == "SPEAKER_01"
    assert c.assign(same_voice, 5.8) == "SPEAKER_01"  # adopted by the weakly-known speaker
    assert c.assign(unit(2), 5.0) == "SPEAKER_02"  # a genuinely new voice still gets a label
    assert c.assign(unit(3), 1.6) in ("SPEAKER_01", "SPEAKER_02")  # too short to found one


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


def words_from(text: str, start: int = 0, ms_per_word: int = 250, pauses: dict[int, int] | None = None):
    """Build Word timings for a sentence; ``pauses`` adds a gap before word index i."""
    out, t = [], start
    for i, w in enumerate(text.split()):
        t += (pauses or {}).get(i, 0)
        out.append(Word(t, t + ms_per_word, " " + w))
        t += ms_per_word
    return out


def run_relabel(words, turns):
    seg = Segment("system", words[0].start_ms, words[-1].end_ms, "".join(w.text for w in words).strip(),
                  0.8, "SPEAKER_01", words)
    out = relabel([seg], {("system", seg.start_ms): words}, {"system": turns}, {"system"})
    return [(s.speaker, s.text) for s in out]


def test_relabel_splits_at_sentence_boundary_on_speaker_change():
    words = words_from("We should ship it on Friday. I disagree, we need one more week of testing.")
    split = words[5].end_ms  # end of "Friday."
    assert run_relabel(words, [Turn(0, split, 7), Turn(split, 99999, 3)]) == [
        ("SPEAKER_01", "We should ship it on Friday."),
        ("SPEAKER_02", "I disagree, we need one more week of testing.")]


def test_relabel_does_not_split_mid_sentence():
    # Test #2: "...we can't do it like right" / "now, which I understand." was split mid-sentence.
    words = words_from("But she said we can't do it like right now, which I understand.")
    boundary = words[8].end_ms  # between "right" and "now,"
    assert run_relabel(words, [Turn(0, boundary, 0), Turn(boundary, 99999, 4)]) == [
        ("SPEAKER_01", "But she said we can't do it like right now, which I understand.")]


def test_relabel_folds_word_level_flip_flops():
    # Test #1: "Who could look at first? I found it. I found it. So this is the one." came out as
    # four segments alternating between two speakers.
    words = words_from("Who could look at first? I found it. I found it. So this is the one.")
    turns = [Turn(0, 1250, 0), Turn(1250, 2000, 2), Turn(2000, 2750, 0), Turn(2750, 99999, 2)]
    out = run_relabel(words, turns)
    assert len(out) <= 2
    assert out[-1][1].endswith("So this is the one.")


def test_relabel_splits_at_pause_without_punctuation():
    words = words_from("okay so that covers the roadmap items we discussed and then the budget review",
                       pauses={9: 600})  # pause before "and"
    split = words[8].end_ms + 300
    out = run_relabel(words, [Turn(0, split, 1), Turn(split, 99999, 5)])
    assert [s for s, _ in out] == ["SPEAKER_01", "SPEAKER_02"]
    assert out[1][1] == "and then the budget review"


def test_cluster_voices_keeps_test3_speakers_apart():
    """Test #3, confirmed by the user: lines 10-12 and 15-18 are two different people.

    Similarities copied from the kept audio (subset: lines 5-8 = A, 10-12 = B, 15-18 = C).
    The old final pass merged B and C.
    """
    from huddle_helper.diarize import cluster_voices

    names = [5, 6, 7, 8, 10, 11, 12, 15, 16, 17, 18]
    sims = {
        5: [1.00, 0.80, 0.79, 0.78, 0.57, 0.63, 0.58, 0.65, 0.70, 0.63, 0.63],
        6: [0.80, 1.00, 0.78, 0.71, 0.56, 0.55, 0.53, 0.57, 0.56, 0.55, 0.54],
        7: [0.79, 0.78, 1.00, 0.82, 0.53, 0.59, 0.53, 0.65, 0.70, 0.65, 0.64],
        8: [0.78, 0.71, 0.82, 1.00, 0.56, 0.61, 0.56, 0.72, 0.80, 0.71, 0.74],
        10: [0.57, 0.56, 0.53, 0.56, 1.00, 0.88, 0.88, 0.59, 0.66, 0.54, 0.55],
        11: [0.63, 0.55, 0.59, 0.61, 0.88, 1.00, 0.83, 0.66, 0.74, 0.60, 0.60],
        12: [0.58, 0.53, 0.53, 0.56, 0.88, 0.83, 1.00, 0.55, 0.68, 0.49, 0.50],
        15: [0.65, 0.57, 0.65, 0.72, 0.59, 0.66, 0.55, 1.00, 0.86, 0.95, 0.90],
        16: [0.70, 0.56, 0.70, 0.80, 0.66, 0.74, 0.68, 0.86, 1.00, 0.83, 0.81],
        17: [0.63, 0.55, 0.65, 0.71, 0.54, 0.60, 0.49, 0.95, 0.83, 1.00, 0.94],
        18: [0.63, 0.54, 0.64, 0.74, 0.55, 0.60, 0.50, 0.90, 0.81, 0.94, 1.00],
    }
    G = np.array([sims[n] for n in names])
    # Recover unit vectors whose dot products reproduce the measured similarities.
    vals, vecs = np.linalg.eigh(G)
    X = vecs * np.sqrt(np.clip(vals, 0, None))
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    durations = [5.8, 4.0, 2.2, 1.6, 10.9, 2.6, 3.3, 11.1, 2.8, 10.0, 9.3]
    labels = cluster_voices(list(X), durations, threshold=0.65)
    group = dict(zip(names, labels))
    assert group[5] == group[6] == group[7] == group[8]
    assert group[10] == group[11] == group[12]
    assert group[15] == group[16] == group[17] == group[18]
    assert len({group[5], group[10], group[15]}) == 3


def test_cluster_voices_and_fold_small():
    from huddle_helper.diarize import cluster_voices, fold_small_clusters

    a, b = unit(0), unit(1)
    odd_a = (a * 0.6 + unit(2) * 0.8)  # a short clip of A that came out unusual (0.6 alike)
    vecs = [a, b, a, odd_a / np.linalg.norm(odd_a), b]
    weights = [10.0, 8.0, 6.0, 1.2, 9.0]
    labels = cluster_voices(vecs, weights, threshold=0.65)
    assert labels[0] == labels[2] and labels[1] == labels[4] and labels[0] != labels[1]
    assert len(set(labels)) == 3  # the odd clip is on its own...
    folded = fold_small_clusters(labels, vecs, weights)
    assert folded[3] == folded[0] and len(set(folded)) == 2  # ...until folded into A
    assert cluster_voices([], [], 0.65) == []


class UnitEmbedder:
    """Fake fingerprints keyed by line start time."""

    def __init__(self, by_start):
        self.by_start = by_start

    def __call__(self, source, start_ms, end_ms):
        for start, vec in self.by_start.items():
            if start <= start_ms < start + 1000 or start_ms <= start < end_ms:
                return vec
        return None


def test_relabel_with_voice_clustering_overrides_diarizer_ids():
    segs = [Segment("system", 0, 4000, "First person speaking here.", 0.9, "SPEAKER_01"),
            Segment("system", 5000, 9000, "Second person answers now.", 0.9, "SPEAKER_01"),
            Segment("system", 10000, 14000, "First person once again.", 0.9, "SPEAKER_01"),
            Segment("system", 14500, 14800, "Hm.", 0.9, "SPEAKER_01")]  # too short to fingerprint
    # The diarizer (wrongly) calls everything speaker 0; the voices say otherwise.
    turns = [Turn(0, 15000, 0)]
    embed = UnitEmbedder({0: unit(0), 5000: unit(1), 10000: unit(0)})
    out = relabel(segs, {}, {"system": turns}, {"system"}, embed=embed)
    assert [s.speaker for s in out] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01", "SPEAKER_01"]


def test_join_fragments():
    from huddle_helper.finalize import join_fragments

    segs = [Segment("mic", 4728, 7228, "Alright, this is to kick off and initiate test number three.", 0.7, "You"),
            Segment("mic", 8824, 10224, "of Huddle Helper.", 0.5, "You"),
            Segment("system", 11320, 12200, "Really want to go deep.", 0.6, "SPEAKER_01"),
            Segment("mic", 100472, 101492, "we're going to close out.", 0.6, "You"),
            Segment("mic", 102360, 102640, "the", 0.4, "You"),
            Segment("mic", 103736, 104576, "Test number three.", 0.5, "You"),
            Segment("mic", 106000, 107000, "New sentence here.", 0.5, "You")]
    out = join_fragments(segs)
    assert [s.text for s in out] == [
        "Alright, this is to kick off and initiate test number three of Huddle Helper.",
        "Really want to go deep.",
        "we're going to close out the Test number three.",
        "New sentence here.",  # previous line ended a sentence and this starts a new one
    ]
    assert out[0].end_ms == 10224 and 0.5 < out[0].confidence < 0.7


def test_join_fragments_respects_speaker_and_gap():
    from huddle_helper.finalize import join_fragments

    segs = [Segment("system", 0, 1000, "and then we", 0.7, "SPEAKER_01"),
            Segment("system", 1500, 2500, "decided to wait.", 0.7, "SPEAKER_02"),
            Segment("system", 6000, 7000, "and later", 0.7, "SPEAKER_02")]
    assert len(join_fragments(segs)) == 3


def test_merge_small_speakers_uses_similarity():
    from huddle_helper.diarize import merge_small_speakers

    turns = [Turn(0, 60000, 0), Turn(60000, 62000, 3), Turn(62000, 120000, 1), Turn(120000, 129000, 2)]
    a, b = unit(0), unit(1)
    near_a = (a * 0.9 + unit(2) * 0.3) / np.linalg.norm(a * 0.9 + unit(2) * 0.3)
    centroids = {0: a, 1: b, 2: near_a, 3: unit(3)}  # 2 sounds like 0; 3 sounds like nobody
    out = merge_small_speakers(turns, centroids)
    assert sorted({t.speaker for t in out}) == [0, 1, 3]
    assert [t for t in out if t.start_ms == 120000][0].speaker == 0


def test_merge_small_speakers_keeps_short_meetings_intact():
    from huddle_helper.diarize import merge_small_speakers

    # Two genuinely different voices, both "small" because the meeting is short.
    turns = [Turn(0, 13000, 0), Turn(13000, 25000, 1)]
    out = merge_small_speakers(turns, {0: unit(0), 1: unit(1)})
    assert {t.speaker for t in out} == {0, 1}


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
    segs = [Segment("mic", 100, 900, "Hello.", 0.9, "You"),
            Segment("system", 500, 1500, "First.", 0.9, "SPEAKER_01", [Word(500, 1500, " First.")]),
            Segment("system", 2500, 3000, "Second.", 0.9, "SPEAKER_01", [Word(2500, 3000, " Second.")])]
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
    assert load_words(folder.words_path)[("system", 2500)][0].text == " Second."
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


def test_finalize_writes_final_stats(tmp_path):
    folder = _make_meeting(tmp_path)
    finalize_meeting(folder, FakeDiarizer, keep_audio=False)
    stats = folder.read_meta()["final_stats"]
    assert stats["audio_s"] == 3.0 and stats["took_s"] >= 0 and stats["speed"] is not None
    assert {"read_audio", "diarize_system", "write"} <= set(stats["stages"])


def test_finalize_honours_keep_audio_set_in_meta_late(tmp_path):
    folder = _make_meeting(tmp_path)  # meta says keep_audio False when the worker read it
    seen = {}

    class LateDiarizer(FakeDiarizer):
        def diarize(self, audio, progress=None):
            folder.update_meta(settings={**folder.read_meta()["settings"], "keep_audio": True})
            seen["done"] = True
            return super().diarize(audio, progress)

    finalize_meeting(folder, LateDiarizer, keep_audio=False)
    assert seen and folder.audio_file("system").exists() and folder.read_meta()["audio_kept"] is True


def test_cmd_finalize_keeps_audio_unless_asked(tmp_path, monkeypatch):
    from huddle_helper import cli

    calls = []
    monkeypatch.setattr(cli, "_finalize", lambda folder, s, keep_audio=None: calls.append(keep_audio))
    folder = _make_meeting(tmp_path)
    assert cli.main(["finalize", str(folder.path), "--out", str(tmp_path)]) == 0
    assert cli.main(["finalize", str(folder.path), "--out", str(tmp_path), "--delete-audio"]) == 0
    assert calls == [True, False]
