"""The dev tool's diff and truth checks, on small synthetic transcripts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import final_lab  # noqa: E402


def seg(start, end, speaker, text="x", source="system"):
    return {"type": "segment", "source": source, "start_ms": start, "end_ms": end,
            "speaker": speaker, "text": text}


def test_diff_ignores_renumbered_labels():
    a = [seg(0, 1000, "SPEAKER_01"), seg(2000, 3000, "SPEAKER_02"), seg(4000, 5000, "SPEAKER_01")]
    b = [seg(0, 1000, "SPEAKER_02"), seg(2000, 3000, "SPEAKER_01"), seg(4000, 5000, "SPEAKER_02")]
    pairs, only_a, only_b = final_lab.match_segments(a, b)
    assert len(pairs) == 3 and not only_a and not only_b
    assert final_lab.moved_pairs(pairs) == [] and final_lab.changed_text_or_bounds(pairs) == []


def test_diff_reports_split_and_merged_speakers():
    a = [seg(0, 1000, "S1"), seg(2000, 3000, "S1"), seg(4000, 5000, "S1"), seg(6000, 7000, "S2")]
    b = [seg(0, 1000, "S1"), seg(2000, 3000, "S1"), seg(4000, 5000, "S3"), seg(6000, 7000, "S1")]
    moved = final_lab.moved_pairs(final_lab.match_segments(a, b)[0])
    assert [x["start_ms"] for x, _y in moved] == [4000, 6000]  # S1 split off a line; S2 merged into S1


def test_diff_reports_text_boundaries_and_unmatched():
    a = [seg(0, 1000, "S1", "one"), seg(2000, 4000, "S1", "two three"), seg(9000, 9500, "S1", "gone")]
    b = [seg(0, 1000, "S1", "uno"), seg(2000, 3000, "S1", "two"), seg(3000, 4000, "S1", "three")]
    pairs, only_a, only_b = final_lab.match_segments(a, b)
    assert [x["start_ms"] for x, _y in final_lab.changed_text_or_bounds(pairs)] == [0, 2000]
    assert [x["start_ms"] for x in only_a] == [9000] and [y["start_ms"] for y in only_b] == [3000]


def test_diff_keeps_sources_apart():
    a = [seg(0, 1000, "You", source="mic")]
    b = [seg(0, 1000, "SPEAKER_01")]
    pairs, only_a, only_b = final_lab.match_segments(a, b)
    assert not pairs and len(only_a) == 1 and len(only_b) == 1


def test_truth_checks():
    pieces = [{"piece": i, "source": "system", "start_ms": i * 1000, "end_ms": i * 1000 + 900, "vec": i}
              for i in range(4)]
    groups = {"A": [["system", 0], ["system", 1000]], "B": [["system", 2000]], "C": [["system", 3500]]}
    resolved = {g: final_lab.resolve_group(items, pieces)[0] for g, items in groups.items()}
    assert [p["piece"] for p in resolved["C"]] == [3]  # nearest start within tolerance
    assert final_lab.resolve_group([["system", 90000]], pieces) == ([], 1)
    import numpy as np
    labels, weights = [0, 0, 1, 1], np.ones(4)
    dists = {g: final_lab.group_clusters(ps, labels, weights) for g, ps in resolved.items()}
    truth = {"together": ["A"], "apart": [["A", "B"], ["B", "C"]]}
    result = dict(final_lab.check_truth(truth, dists))
    assert result == {"together A": True, "apart A/B": True, "apart B/C": False}


def test_unchecked_groups_are_not_checked():
    truth = {"together": ["A", "X"], "apart": [["A", "B"], ["A", "X"]], "unchecked": ["X"]}
    dists = {"A": {0: 5.0}, "B": {1: 5.0}, "X": {0: 3.0, 1: 3.0}}
    assert dict(final_lab.check_truth(truth, dists)) == {"together A": True, "apart A/B": True}


def test_anchors_first_places_short_pieces():
    import numpy as np

    def unit(i):
        v = np.zeros(4)
        v[i] = 1.0
        return v

    odd = (unit(0) * 0.6 + unit(2) * 0.8)
    vecs = [unit(0), unit(1), unit(0), unit(1), odd, unit(3)]
    weights = [6.0, 6.0, 6.0, 6.0, 1.5, 1.5]
    starts = [0, 10_000, 20_000, 30_000, 21_000, 29_000]
    labels = final_lab.anchors_first(vecs, weights, starts, 0.70, 3.0, 0.55)
    assert labels[0] == labels[2] == labels[4]  # 0.6 like the first voice: taken at assign 0.55
    assert labels[1] == labels[3] == labels[5]  # unlike either: takes its nearest neighbour in time
    assert labels[0] != labels[1]
    strict = final_lab.anchors_first(vecs, weights, starts, 0.70, 3.0, 0.65)
    assert strict[4] == strict[2]  # not alike enough, so the neighbour rule gives the same answer here
