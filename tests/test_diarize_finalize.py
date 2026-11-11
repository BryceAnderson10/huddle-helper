import json

import numpy as np

from huddle_helper import diarize
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.diarize import OnlineClusterer, Turn
from huddle_helper.finalize import finalize_meeting, load_words, relabel, speaker_at
from huddle_helper.segments import Segment, Word
from huddle_helper.writer import read_events

from .conftest import FakeDiarizer, make_meeting_to_finalize


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


def _blend(i, j, toward_i):
    """Unit vector whose cosine with unit(i) is ``toward_i``, the rest along unit(j)."""
    v = unit(i) * toward_i + unit(j) * float(np.sqrt(1 - toward_i ** 2))
    return v / np.linalg.norm(v)


def _fold_case(candidate_s, candidate_cos, **kw):
    from huddle_helper.diarize import fold_small_clusters

    vecs = [unit(0), unit(1), _blend(0, 2, candidate_cos)]
    return fold_small_clusters([0, 1, 2], vecs, [100.0, 100.0, candidate_s], **kw)


def test_fold_small_and_similar_folds():
    assert _fold_case(5.0, 0.80) == [0, 1, 0]  # over 4 s, but 0.80 like cluster 0


def test_fold_small_and_different_stays():
    assert _fold_case(5.0, 0.50) == [0, 1, 2]  # Test #4's 28 s speaker: 0.64-0.75 like its target


def test_fold_under_always_fold_s_folds_whatever_the_similarity():
    assert _fold_case(3.0, 0.30) == [0, 1, 0]


def test_fold_protected_never_folds():
    assert _fold_case(3.0, 0.80, protected={2}) == [0, 1, 2]
    assert _fold_case(5.0, 0.80, protected={2}) == [0, 1, 2]


def test_fold_threshold_is_the_target_similarity():
    assert _fold_case(5.0, 0.80, min_similarity=0.85) == [0, 1, 2]


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
            Segment("system", 2500, 3500, "decided to wait.", 0.7, "SPEAKER_02"),  # 1.5 s: too far to attach
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


def test_finalize_writes_final_then_done_and_removes_audio(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
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
    folder = make_meeting_to_finalize(tmp_path, keep_audio=True)
    finalize_meeting(folder, FakeDiarizer, keep_audio=True)
    assert folder.audio_file("system").exists() and folder.read_meta()["audio_kept"] is True


def test_finalize_survives_diarizer_failure(tmp_path):
    def broken():
        raise RuntimeError("model missing")

    folder = make_meeting_to_finalize(tmp_path)
    finalize_meeting(folder, broken, keep_audio=False)
    speakers = [e["speaker"] for e in read_events(folder.final_path) if e["type"] == "segment"]
    assert speakers == ["You", "SPEAKER_01", "SPEAKER_01"]
    assert folder.is_done


def test_speakers_json(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    folder.write_speakers({"SPEAKER_01": " Dana ", "SPEAKER_02": "", "You": "You"})
    data = json.loads(folder.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"] == {"SPEAKER_01": "Dana"}
    assert folder.read_speakers() == {"SPEAKER_01": "Dana"}


def test_finalize_writes_final_stats(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    finalize_meeting(folder, FakeDiarizer, keep_audio=False)
    stats = folder.read_meta()["final_stats"]
    assert stats["audio_s"] == 3.0 and stats["took_s"] >= 0 and stats["speed"] is not None
    assert {"read_audio", "diarize_system", "write"} <= set(stats["stages"])


def test_finalize_honours_keep_audio_set_in_meta_late(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)  # meta says keep_audio False when the worker read it
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
    folder = make_meeting_to_finalize(tmp_path)
    assert cli.main(["finalize", str(folder.path), "--out", str(tmp_path)]) == 0
    assert cli.main(["finalize", str(folder.path), "--out", str(tmp_path), "--delete-audio"]) == 0
    assert calls == [True, False]


def test_voice_label_steps_match_combined_helper():
    from huddle_helper.diarize import profile
    from huddle_helper.finalize import _voice_labels, assign_labels, embed_pieces, label_voices

    def pieces():
        spans = [(0, 4000), (5000, 9000), (10000, 14000), (14500, 14800)]  # the last is too short
        return [(min(i, 2), Segment("system", a, b, "x", 0.9, "#0")) for i, (a, b) in enumerate(spans)]

    embed = UnitEmbedder({0: unit(0), 5000: unit(1), 10000: unit(0)})
    whole, timings = pieces(), {}
    _voice_labels(whole, embed, profile(final_match=0.65), timings)
    assert set(timings) == {"embed_pieces", "cluster"}

    steps = pieces()
    vecs, weights, idx = embed_pieces(steps, embed)
    assert idx == [0, 1, 2] and weights == [4.0, 4.0, 4.0]
    labels = label_voices(vecs, weights, 0.65)
    assert labels[0] == labels[2] != labels[1]
    assign_labels(steps, idx, labels)
    assert [s.speaker for _l, s in steps] == [s.speaker for _l, s in whole] == ["#v0", "#v1", "#v0", "#v0"]


def test_split_pieces_passes_undiarized_and_failed_streams_through():
    from huddle_helper.finalize import split_pieces

    segs = [Segment("mic", 0, 1000, "hi", 0.9, "You"), Segment("system", 0, 1000, "yo", 0.9, "SPEAKER_04")]
    out, pieces = split_pieces(segs, {}, {"system": None}, {"system"}, embed_available=False)
    assert [s.speaker for s in out] == ["You", "SPEAKER_04"] and pieces == {}
    out, pieces = split_pieces(segs, {}, {}, set(), embed_available=True)
    assert [s.speaker for s in out] == ["You", "Remote"] and pieces == {}
    out, pieces = split_pieces(segs, {}, {"system": None}, {"system"}, embed_available=True)
    assert [s.speaker for s in out] == ["You"]
    assert [(line, p.speaker) for line, p in pieces["system"]] == [(1, "SPEAKER_04")]


def test_finalize_times_embedding_and_clustering_separately(tmp_path):
    class FlatEmbedder:
        def embed(self, audio):
            return unit(0)

    folder = make_meeting_to_finalize(tmp_path)
    finalize_meeting(folder, FakeDiarizer, keep_audio=False, embedder_factory=FlatEmbedder)
    stages = folder.read_meta()["final_stats"]["stages"]
    assert {"embed_pieces", "cluster"} <= set(stages) and "voice_labels" not in stages


def _seg(start, end, text, speaker="SPEAKER_01", source="system"):
    return Segment(source, start, end, text, 0.8, speaker)


def test_join_fragments_ellipsis_is_not_a_sentence_end():
    from huddle_helper.finalize import join_fragments

    segs = [_seg(0, 3000, "We could always set up the…"),
            _seg(3600, 5000, "Blue widget side of the process.")]
    out = join_fragments(segs)
    assert [s.text for s in out] == ["We could always set up the… Blue widget side of the process."]
    assert [s.text for s in join_fragments([_seg(0, 3000, "We could set up the..."), segs[1]])] == [
        "We could set up the... Blue widget side of the process."]


def test_join_fragments_attaches_short_tail_of_another_speaker():
    from huddle_helper.finalize import join_fragments

    segs = [_seg(0, 4000, "Is it just a list of the", "SPEAKER_03"),
            _seg(4400, 5000, "Red table.", "SPEAKER_01"),
            _seg(9000, 12000, "A new thought entirely.", "SPEAKER_01")]
    out = join_fragments(segs)
    assert [(s.speaker, s.text) for s in out] == [
        ("SPEAKER_03", "Is it just a list of the Red table."), ("SPEAKER_01", "A new thought entirely.")]
    assert out[0].end_ms == 5000


def test_join_fragments_cross_speaker_attach_has_limits():
    from huddle_helper.finalize import join_fragments

    head = _seg(0, 4000, "Is it just a list of the", "SPEAKER_03")
    assert len(join_fragments([head, _seg(5200, 6000, "Red table.", "SPEAKER_01")])) == 2  # gap 1.2 s
    assert len(join_fragments([head, _seg(4400, 6000, "The red table here.", "SPEAKER_01")])) == 2  # 4 words
    closed = _seg(0, 4000, "Is it just a list of them.", "SPEAKER_03")
    assert len(join_fragments([closed, _seg(4400, 5000, "Red table.", "SPEAKER_01")])) == 2
    other_source = _seg(4400, 5000, "Red table.", "You", source="mic")
    assert len(join_fragments([head, other_source])) == 2


def test_join_fragments_does_not_attach_reply_after_trailing_off():
    from huddle_helper.finalize import join_fragments

    # Rule (ii) needs a line that stops mid-sentence; "…" is someone trailing off, and the short
    # line after it is usually another person's reply (Test #4).
    segs = [Segment("system", 0, 2000, "Right, so…", 0.9, "SPEAKER_01"),
            Segment("system", 2300, 3000, "Sounds good.", 0.9, "SPEAKER_02")]
    out = join_fragments(segs)
    assert [(s.speaker, s.text) for s in out] == [("SPEAKER_01", "Right, so…"), ("SPEAKER_02", "Sounds good.")]


def test_final_threads_half_the_cores_up_to_four_and_one_while_recording():
    from huddle_helper.diarize import final_threads

    assert [final_threads(False, n) for n in (1, 2, 4, 6, 8, 16)] == [1, 1, 2, 3, 4, 4]
    assert final_threads(True, 16) == 1
    assert 1 <= final_threads() <= 4


def test_embed_pieces_reports_progress():
    from huddle_helper.finalize import embed_pieces

    steps = [(i, Segment("system", i * 5000, i * 5000 + 4000, "x", 0.9, "#0")) for i in range(4)]
    seen = []
    embed_pieces(steps, UnitEmbedder({}), seen.append)
    assert seen[:4] == [0.0, 0.25, 0.5, 0.75] and seen[-1] == 1.0
    embed_pieces([], UnitEmbedder({}), seen.append)  # nothing to embed: no division by zero


def test_finalize_reports_grouping_progress_and_merge_small_stage(tmp_path):
    class FlatEmbedder:
        def embed(self, audio):
            return unit(0)

    class TimedDiarizer(FakeDiarizer):
        last_timings = {"merge_small": 1.5}

    folder = make_meeting_to_finalize(tmp_path)
    seen = []
    finalize_meeting(folder, TimedDiarizer, keep_audio=False, embedder_factory=FlatEmbedder,
                     progress=lambda stage, frac: seen.append((stage, frac)))
    grouping = [f for stage, f in seen if stage == "Grouping voices"]
    assert grouping[0] == 0.0 and grouping[-1] == 1.0 and grouping == sorted(grouping) and len(grouping) > 2
    stages = folder.read_meta()["final_stats"]["stages"]
    assert stages["merge_small_system"] == 1.5 and "diarize_system" in stages


def test_finalize_works_with_a_diarizer_without_last_timings(tmp_path):
    folder = make_meeting_to_finalize(tmp_path)
    assert not hasattr(FakeDiarizer(), "last_timings")
    finalize_meeting(folder, FakeDiarizer, keep_audio=False)
    stages = folder.read_meta()["final_stats"]["stages"]
    assert "diarize_system" in stages and not any(k.startswith("merge_small") for k in stages)


def test_voice_profiles_and_overrides():
    """WP13: every cutoff belongs to a fingerprint model; a number given by hand replaces one value."""
    from huddle_helper import models
    from huddle_helper.diarize import PROFILES, SHIPPED_UNTIL_WP13, profile

    shipped = profile()
    assert shipped.model == models.EMBEDDING_NAME and shipped is PROFILES[models.EMBEDDING_NAME]
    old = profile(SHIPPED_UNTIL_WP13)
    assert (old.live_match, old.live_adopt, old.final_match, old.fold_min, old.tag_gate) == (0.75, 0.6, 0.70, 0.70, 0.70)
    assert shipped.live_adopt < shipped.live_match and shipped.fold_min == shipped.final_match
    changed = profile(final_match=0.5, live_match=None)
    assert changed.final_match == 0.5 and changed.live_match == shipped.live_match
    assert profile(final_match=None) is shipped


def test_recorder_live_cutoffs_come_from_the_profile():
    from huddle_helper.config import Settings
    from huddle_helper.diarize import profile

    assert Settings().voice_profile() == profile()
    s = Settings(live_speaker_threshold=0.9, final_cluster_threshold=0.7)
    assert (s.voice_profile().live_match, s.voice_profile().split_cluster) == (0.9, 0.7)
    assert s.voice_profile().final_match == profile().final_match


def test_cluster_voices_test3_with_the_shipped_model():
    """Test #3 again with the model shipped since WP13 (A = 6 lines, B = 3, C = 4; confirmed apart).

    Similarities copied from the kept audio. The three speakers must come out at the shipped
    cutoff and 0.08 either side of it: the old model passed only from 0.70 to 0.71.
    """
    from huddle_helper.diarize import cluster_voices, fold_small_clusters, profile

    groups = "AAAAAABBBCCCC"
    G = np.array([
        [1.00, 0.59, 0.63, 0.61, 0.42, 0.64, 0.14, 0.06, 0.17, 0.11, 0.19, 0.10, 0.09],
        [0.59, 1.00, 0.68, 0.63, 0.43, 0.61, 0.13, 0.11, 0.20, 0.09, 0.18, 0.09, 0.08],
        [0.63, 0.68, 1.00, 0.73, 0.44, 0.57, 0.24, 0.21, 0.24, 0.13, 0.25, 0.12, 0.09],
        [0.61, 0.63, 0.73, 1.00, 0.37, 0.63, 0.23, 0.24, 0.26, 0.15, 0.21, 0.08, 0.06],
        [0.42, 0.43, 0.44, 0.37, 1.00, 0.59, 0.16, 0.08, 0.18, 0.09, 0.12, -0.05, 0.04],
        [0.64, 0.61, 0.57, 0.63, 0.59, 1.00, 0.14, 0.12, 0.22, 0.17, 0.20, 0.09, 0.13],
        [0.14, 0.13, 0.24, 0.23, 0.16, 0.14, 1.00, 0.70, 0.73, 0.07, 0.13, 0.07, 0.05],
        [0.06, 0.11, 0.21, 0.24, 0.08, 0.12, 0.70, 1.00, 0.61, 0.06, 0.06, 0.04, 0.10],
        [0.17, 0.20, 0.24, 0.26, 0.18, 0.22, 0.73, 0.61, 1.00, 0.04, 0.09, -0.01, -0.02],
        [0.11, 0.09, 0.13, 0.15, 0.09, 0.17, 0.07, 0.06, 0.04, 1.00, 0.75, 0.83, 0.79],
        [0.19, 0.18, 0.25, 0.21, 0.12, 0.20, 0.13, 0.06, 0.09, 0.75, 1.00, 0.69, 0.66],
        [0.10, 0.09, 0.12, 0.08, -0.05, 0.09, 0.07, 0.04, -0.01, 0.83, 0.69, 1.00, 0.88],
        [0.09, 0.08, 0.09, 0.06, 0.04, 0.13, 0.05, 0.10, -0.02, 0.79, 0.66, 0.88, 1.00],
    ])
    vals, vecs = np.linalg.eigh(G)
    X = vecs * np.sqrt(np.clip(vals, 0, None))
    X = list(X / np.linalg.norm(X, axis=1, keepdims=True))
    durations = [5.8, 4.0, 2.2, 1.6, 2.2, 3.9, 10.9, 2.6, 3.3, 11.1, 2.8, 10.0, 9.3]
    centre = profile().final_match
    for t in (centre - 0.08, centre, centre + 0.08):
        labels = fold_small_clusters(cluster_voices(X, durations, threshold=t), X, durations, min_similarity=t)
        by_group = {g: {lab for lab, x in zip(labels, groups) if x == g} for g in "ABC"}
        assert all(len(v) == 1 for v in by_group.values()), (t, by_group)
        assert len(set.union(*by_group.values())) == 3, (t, by_group)


def test_the_embedder_gives_no_fingerprint_below_the_shared_minimum(monkeypatch):
    embedder = object.__new__(diarize.SherpaEmbedder)  # no model: short audio returns before using it
    assert embedder.embed(np.zeros(SAMPLE_RATE // 2 - 1, dtype=np.float32)) is None
    monkeypatch.setattr(diarize, "MIN_FINGERPRINT_S", 1.0)  # the picker's note (#26) reads the same constant
    assert embedder.embed(np.zeros(int(0.7 * SAMPLE_RATE), dtype=np.float32)) is None
