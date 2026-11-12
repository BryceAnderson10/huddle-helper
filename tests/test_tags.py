"""Naming speakers (WP8): the reducer, the rules before clustering, the mapping after, merge_speakers,
and the order of the files at the end of the final pass. Names here are invented (Ana, Ben, Cy)."""

import json
import re
import threading

import numpy as np
import soundfile as sf

from huddle_helper import meeting as mt, tags
from huddle_helper.diarize import cluster_voices
from huddle_helper.finalize import append_words, finalize_meeting, label_voices
from huddle_helper.segments import Segment, Word
from huddle_helper.writer import JsonlWriter, meeting_end_event, meeting_start_event, read_events


def sp(label, name, at="t"):
    return {"type": "tag", "at": at, "scope": "speaker", "label": label, "name": name}


def ln(source, start, name, end=None):
    return {"type": "tag", "at": "t", "scope": "line", "source": source, "start_ms": start,
            "end_ms": end or start + 1000, "name": name}


# -- reducer ---------------------------------------------------------------------------
def test_reduce_tag_untag_and_later_wins():
    s = tags.reduce([sp("SPEAKER_01", "Ana"), sp("SPEAKER_02", "Ben"), sp("SPEAKER_01", "Cy")])
    assert s.speakers == {"SPEAKER_01": "Cy", "SPEAKER_02": "Ben"}
    s = tags.reduce([{"type": "untag", "scope": "speaker", "label": "SPEAKER_02"}], s)
    assert s.speakers == {"SPEAKER_01": "Cy"}
    assert bool(s) and not bool(tags.reduce([]))


def test_reduce_line_name_beats_speaker_name_and_untag_restores_it():
    s = tags.reduce([sp("SPEAKER_01", "Ana"), ln("system", 5000, "Ben")])
    assert s.name_of("SPEAKER_01", "system", 5000) == "Ben"
    assert s.name_of("SPEAKER_01", "system", 9000) == "Ana"
    tags.reduce([{"type": "untag", "scope": "line", "source": "system", "start_ms": 5000}], s)
    assert s.name_of("SPEAKER_01", "system", 5000) == "Ana"


def test_reduce_ignores_malformed_events_and_other_types():
    s = tags.reduce([{"type": "mark", "id": 1}, {"type": "tag", "scope": "line"},
                     {"type": "tag", "scope": "speaker", "name": "Ana"}, {"type": "tag", "scope": "speaker",
                                                                          "label": "X", "name": "  "}])
    assert not s


def test_event_constructors_round_trip():
    s = tags.reduce([tags.tag_speaker_event("SPEAKER_01", "Ana"), tags.tag_line_event("system", 10, 20, "Ben", seq=3)])
    assert s.speakers == {"SPEAKER_01": "Ana"} and s.lines == {("system", 10): ("Ben", 20)}
    s = tags.reduce([tags.tag_speaker_event("SPEAKER_01", None), tags.tag_line_event("system", 10, 20, "")], s)
    assert not s
    assert s.names() == []


# -- ranges and mapping ----------------------------------------------------------------
def seg(source, start, end, speaker, text="x"):
    return Segment(source, start, end, text, 0.9, speaker)


def test_tagged_ranges_line_beats_speaker():
    live = [seg("system", 0, 1000, "SPEAKER_01"), seg("system", 2000, 3000, "SPEAKER_01"),
            seg("system", 4000, 5000, "SPEAKER_02")]
    st = tags.reduce([sp("SPEAKER_01", "Ana"), ln("system", 2000, "Ben", 3000)])
    r = tags.tagged_ranges(st, live)
    assert [(x.start_ms, x.name, x.scope) for x in r] == [(0, "Ana", "speaker"), (2000, "Ben", "line")]


def test_map_final_speakers_largest_overlap_wins_and_25_percent_is_a_conflict():
    ranges = [tags.TaggedRange("system", 0, 10_000, "Ana", "speaker"),
              tags.TaggedRange("system", 10_000, 12_500, "Ben", "line"),  # 2500 of 10000: exactly 25%
              tags.TaggedRange("system", 20_000, 30_000, "Cy", "speaker"),
              tags.TaggedRange("system", 30_000, 32_000, "Ana", "line")]  # 20%: no conflict
    final = [seg("system", 0, 12_500, "SPEAKER_01"), seg("system", 20_000, 32_000, "SPEAKER_02"),
             seg("system", 40_000, 41_000, "SPEAKER_03")]
    mapping, conflicts = tags.map_final_speakers(final, ranges)
    assert mapping == {"SPEAKER_01": "Ana", "SPEAKER_02": "Cy"}  # SPEAKER_03 has no tagged speech
    assert conflicts == [{"speaker": "SPEAKER_01", "names": {"Ana": 10_000, "Ben": 2_500}}]


def test_map_final_speakers_just_under_25_percent_is_no_conflict():
    ranges = [tags.TaggedRange("system", 0, 10_000, "Ana", "line"), tags.TaggedRange("system", 10_000, 12_499, "Ben", "line")]
    mapping, conflicts = tags.map_final_speakers([seg("system", 0, 20_000, "SPEAKER_01")], ranges)
    assert mapping == {"SPEAKER_01": "Ana"} and conflicts == []


def test_map_final_speakers_streams_are_separate_and_you_can_be_named():
    ranges = [tags.TaggedRange("mic", 0, 5000, "Ben", "speaker"), tags.TaggedRange("system", 0, 5000, "Ana", "speaker")]
    final = [seg("mic", 0, 5000, "You"), seg("system", 0, 5000, "SPEAKER_01")]
    mapping, conflicts = tags.map_final_speakers(final, ranges)
    assert mapping == {"You": "Ben", "SPEAKER_01": "Ana"} and not conflicts


# -- rules before clustering ---------------------------------------------------------------
def vec(theta, n=3):
    v = np.zeros(n)
    v[0], v[1] = np.cos(theta), np.sin(theta)
    return v


def test_cluster_voices_cannot_link_between_differently_named_clusters():
    # Two voices at 0.9 similarity: they merge on their own, and stay apart when named differently.
    theta = float(np.arccos(0.9))
    vecs = [vec(0), vec(0.05), vec(theta), vec(theta + 0.05)]
    w = [3.0] * 4
    assert len(set(cluster_voices(vecs, w, 0.70))) == 1
    labels = cluster_voices(vecs, w, 0.70, names=["Ana", None, "Ben", None])
    assert labels[0] != labels[2]
    assert len(set(labels)) == 2
    # No names, or all None: the same as before.
    assert cluster_voices(vecs, w, 0.70, names=[None] * 4) == cluster_voices(vecs, w, 0.70)


def test_cluster_voices_same_name_may_merge_and_an_unnamed_cluster_may_join_either():
    theta = float(np.arccos(0.9))
    vecs = [vec(0), vec(0.02), vec(theta), vec(theta / 2)]
    names = ["Ana", "Ana", "Ben", None]
    labels = cluster_voices(vecs, [2.0] * 4, 0.70, names)
    assert labels[0] == labels[1] and labels[0] != labels[2]
    assert labels[3] in (labels[0], labels[2])  # the unnamed piece goes to one of them


def test_same_name_pieces_that_plain_clustering_separates_end_up_together():
    # Two voices 90 degrees apart (cos 0): plain clustering keeps them apart; the same name joins them.
    vecs = [vec(0), vec(0.02), vec(np.pi / 2), vec(np.pi / 2 + 0.02)]
    w = [3.0] * 4
    plain = cluster_voices(vecs, w, 0.70)
    assert plain[0] == plain[1] and plain[2] == plain[3] and plain[0] != plain[2]
    labels = cluster_voices(vecs, w, 0.70, names=["Ana", None, "Ana", None])
    assert labels[0] == labels[2]  # the unnamed neighbours are only ~0.5 like the joined cluster: left alone
    assert labels[1] != labels[0] and labels[3] != labels[0]
    # Even when the user is plainly wrong about it, and both pieces are alone.
    assert cluster_voices([vec(0), vec(np.pi)], [1.0, 1.0], 0.70, ["Ana", "Ana"]) == [0, 0]


def test_same_name_pre_merge_is_the_state_the_loop_would_have_made():
    # Ana's two pieces are close enough that plain linkage merges them anyway: naming them changes nothing.
    vecs = [vec(0.0), vec(0.1), vec(0.55), vec(1.2)]
    w = [1.0, 5.0, 1.0, 1.0]
    plain = cluster_voices(vecs, w, 0.8)
    assert plain[0] == plain[1]
    assert cluster_voices(vecs, w, 0.8, ["Ana", "Ana", None, None]) == plain


def test_different_names_still_never_merge_when_a_same_name_pre_merge_makes_centroids_closer():
    # Ana's two pieces sit either side of Ben's piece, so the merged Ana centroid points at Ben (cos ~0.97).
    vecs = [vec(-0.3), vec(0.3), vec(0.0), vec(0.02)]
    labels = cluster_voices(vecs, [2.0] * 4, 0.70, ["Ana", "Ana", "Ben", None])
    assert labels[0] == labels[1] and labels[0] != labels[2]
    assert labels[3] == labels[2]  # the unnamed piece is nearest Ben
    # Ben named on the near piece and Cy on the other: nothing merges across names.
    labels = cluster_voices(vecs, [2.0] * 4, 0.70, ["Ana", "Ana", "Ben", "Cy"])
    assert len({labels[0], labels[2], labels[3]}) == 3 and labels[0] == labels[1]


def test_no_names_or_all_none_is_identical_with_the_pre_merge_in_place():
    rng = np.random.default_rng(7)
    vecs = [v / np.linalg.norm(v) for v in rng.normal(size=(30, 8)) + rng.normal(size=(1, 8)) * 2]
    w = list(rng.uniform(0.5, 6, 30))
    base = cluster_voices(vecs, w, 0.30)
    assert cluster_voices(vecs, w, 0.30, None) == base
    assert cluster_voices(vecs, w, 0.30, [None] * 30) == base


def test_gated_out_speaker_tag_pieces_stay_unnamed_so_a_mixed_live_label_does_not_force_them_together():
    # A live label tagged Ana that mixes two voices: 4 pieces of one, 1 of another (cos 0.2). The odd piece is
    # gated out, stays unnamed, and the pre-merge does not drag it into Ana's cluster.
    specs = [("system", i * 5000, i * 5000 + 4000) for i in range(5)]
    pieces = make_pieces(specs)
    vecs = [vec(0.0), vec(0.03), vec(0.06), vec(0.09), vec(np.arccos(0.2))]
    w = [4.0] * 5
    live = [seg("system", a, b, "SPEAKER_01") for _s, a, b in specs]
    ranges = tags.tagged_ranges(tags.reduce([sp("SPEAKER_01", "Ana")]), live)
    names = tags.piece_names(pieces, list(range(5)), vecs, w, ranges)
    assert names == ["Ana"] * 4 + [None]
    labels = cluster_voices(vecs, w, 0.70, names)
    assert labels[0] == labels[1] == labels[2] == labels[3] and labels[4] != labels[0]
    # The same odd piece named by line is a rule: it is forced into Ana's cluster.
    ranges = tags.tagged_ranges(tags.reduce([sp("SPEAKER_01", "Ana"), ln("system", 20_000, "Ana", 24_000)]), live)
    names = tags.piece_names(pieces, list(range(5)), vecs, w, ranges)
    assert names == ["Ana"] * 5
    assert set(cluster_voices(vecs, w, 0.70, names)) == {0}


def test_cannot_link_survives_merges_through_an_unnamed_cluster():
    # Ana - u - Ben chain: u joins one side; the merged cluster can never take the other name.
    theta = 0.45
    vecs = [vec(0), vec(theta / 2), vec(theta)]
    labels = cluster_voices(vecs, [1.0, 1.0, 1.0], 0.5, ["Ana", None, "Ben"])
    assert labels[0] != labels[2]


def test_label_voices_protects_named_small_clusters_from_the_fold():
    big = [vec(0.0), vec(0.02), vec(0.04), vec(0.03)]
    small = [vec(1.4)]
    vecs = big + small
    w = [20.0] * 4 + [2.0]  # 2 s: under always_fold_s, so it would fold into the big cluster
    plain = label_voices(vecs, w, 0.70)
    assert len(set(plain)) == 1
    named = label_voices(vecs, w, 0.70, names=[None, None, None, None, "Ben"])
    assert named[4] != named[0]


def make_pieces(specs):
    return [(i, Segment(src, s, e, "x", 0.9, "#0")) for i, (src, s, e) in enumerate(specs)]


def test_piece_names_line_tag_always_counts_and_speaker_tag_needs_0_70():
    # Four pieces under one speaker-level name; the last one sounds like somebody else (cos 0.2).
    specs = [("system", 0, 4000), ("system", 5000, 9000), ("system", 10_000, 14_000),
             ("system", 15_000, 19_000)]
    pieces = make_pieces(specs)
    vecs = [vec(0.0), vec(0.05), vec(0.1), vec(np.arccos(0.2))]
    w = [4.0] * 4
    live = [seg("system", a, b, "SPEAKER_01") for _s, a, b in specs]
    ranges = tags.tagged_ranges(tags.reduce([sp("SPEAKER_01", "Ana")]), live)
    assert tags.piece_names(pieces, [0, 1, 2, 3], vecs, w, ranges) == ["Ana", "Ana", "Ana", None]
    # The same odd piece named by line is always a rule.
    ranges = tags.tagged_ranges(tags.reduce([sp("SPEAKER_01", "Ana"), ln("system", 15_000, "Ben", 19_000)]), live)
    assert tags.piece_names(pieces, [0, 1, 2, 3], vecs, w, ranges) == ["Ana", "Ana", "Ana", "Ben"]


def two_voices_under_one_label():
    """Test #7's shape: one live label named Ana holds 3 pieces of Ana and 3 of Ben; one of Ben's is named
    Ben by line. Ben's other two pass a 0.40 gate against Ana's centroid (about 0.74), which they pull
    towards themselves, but they are exactly like Ben's named line."""
    specs = [("system", i * 5000, i * 5000 + 4000) for i in range(6)]
    pieces = make_pieces(specs)
    vecs = [vec(0.0), vec(0.0), vec(0.0), vec(1.2), vec(1.2), vec(1.2)]
    w = [4.0] * 6
    live = [seg("system", a, b, "SPEAKER_01") for _s, a, b in specs]
    ranges = tags.tagged_ranges(tags.reduce([sp("SPEAKER_01", "Ana"), ln("system", 25_000, "Ben", 29_000)]), live)
    return pieces, vecs, w, ranges


def test_piece_names_speaker_name_gives_way_to_a_line_name_the_piece_sounds_more_like():
    pieces, vecs, w, ranges = two_voices_under_one_label()
    assert tags.piece_names(pieces, list(range(6)), vecs, w, ranges, gate=0.40) == \
        ["Ana", "Ana", "Ana", None, None, "Ben"]


def test_pieces_that_sound_like_a_line_name_cluster_with_it_not_with_the_speaker_name():
    pieces, vecs, w, ranges = two_voices_under_one_label()
    names = tags.piece_names(pieces, list(range(6)), vecs, w, ranges, gate=0.40)
    labels = label_voices(vecs, w, 0.40, names, 0.40)
    assert labels[3] == labels[4] == labels[5] != labels[0]
    assert labels[0] == labels[1] == labels[2]


def test_piece_names_uses_largest_overlap_same_stream_and_skips_unfingerprinted():
    pieces = make_pieces([("system", 0, 3000), ("mic", 0, 3000), ("system", 8000, 9000)])
    ranges = [tags.TaggedRange("system", 0, 1000, "Ana", "line"), tags.TaggedRange("system", 1000, 3000, "Ben", "line")]
    names = tags.piece_names(pieces, [0, 2], [vec(0), vec(0)], [3.0, 1.0], ranges)  # piece 1 has no vector
    assert names == ["Ben", None]
    assert tags.piece_names(pieces, [0], [vec(0)], [3.0], []) == [None]


# -- merge_speakers --------------------------------------------------------------------------
def folder_with(tmp_path):
    return mt.MeetingFolder.create(tmp_path, "Merge", mt.now_local(), {})


def test_merge_speakers_keeps_a_downstream_name_for_a_speaker_the_user_did_not_name(tmp_path):
    f = folder_with(tmp_path)
    mt.write_json_atomic(f.speakers_path, {"speakers": {"SPEAKER_02": "Downstream Dan", "SPEAKER_01": "Old"},
                                           "extra": 1, "updated_at": "x"})
    data = mt.merge_speakers(f, {"SPEAKER_01": "Ana"}, from_tags=True,
                             conflicts=[{"speaker": "SPEAKER_01", "names": {"Ana": 5, "Ben": 2}}])
    assert f.read_speakers() == {"SPEAKER_01": "Ana", "SPEAKER_02": "Downstream Dan"}
    assert data["from_tags"] is True and data["extra"] == 1 and data["conflicts"][0]["speaker"] == "SPEAKER_01"
    # A later name from the final view settles that conflict and removes a name on request.
    mt.merge_speakers(f, {"SPEAKER_01": "Ben", "SPEAKER_02": None})
    data = json.loads(f.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"] == {"SPEAKER_01": "Ben"} and data["conflicts"] == [] and data["from_tags"] is True


def test_merge_speakers_from_two_threads_loses_nothing(tmp_path):
    f = folder_with(tmp_path)
    ts = [threading.Thread(target=lambda i=i: mt.merge_speakers(mt.MeetingFolder(f.path), {f"SPEAKER_{i:02d}": f"N{i}"}))
          for i in range(1, 9)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(f.read_speakers()) == 8


# -- the final pass --------------------------------------------------------------------------
class AmpEmbedder:
    """The voice of a clip is read from its level: theta = mean * 10."""

    def embed(self, audio):
        theta = float(np.mean(audio)) * 10
        return np.array([np.cos(theta), np.sin(theta), 0.0], np.float32)


class NoTurns:
    def diarize(self, audio, progress=None):
        return []


THETAS = [0.0, 0.45, 0.05]  # A and B are 0.9 alike; C is A's twin
STARTS = [0, 3000, 6000]


def build(tmp_path, tag_events=None, name="Fx"):
    started = mt.now_local().replace(year=2026, month=1, day=15, hour=9, minute=30, second=0, microsecond=0)  # fixed: files compare byte for byte
    folder = mt.MeetingFolder.create(tmp_path, name, started, {
        "settings": {"diarize_system": True, "diarize_mic": False, "keep_audio": False}})
    audio = np.zeros(16000 * 9, np.float32)
    segs = []
    for th, start in zip(THETAS, STARTS):
        audio[start * 16:(start + 2000) * 16] = th / 10
        segs.append(Segment("system", start, start + 2000, "Words here.", 0.9, "SPEAKER_01",
                            [Word(start, start + 2000, " Words here.")]))
    segs.insert(0, Segment("mic", 100, 900, "Hello.", 0.9, "You"))
    sf.write(str(folder.audio_file("system")), audio, 16000, format="FLAC")
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, mt.iso(started)))
        for i, s in enumerate(segs, 1):
            w.write(s.to_event(i))
            append_words(folder.words_path, s)
        w.write(meeting_end_event(mt.iso(started), len(segs)))
    for e in tag_events or []:
        mt.append_jsonl(folder.tags_path, e)
    folder.update_meta(status=mt.STATUS_PROCESSING, clean_exit=True)
    return folder


def run(folder):
    finalize_meeting(folder, NoTurns, keep_audio=False, embedder_factory=AmpEmbedder)
    return [(e["start_ms"], e["speaker"]) for e in read_events(folder.final_path) if e["type"] == "segment"]


def test_finalize_without_names_merges_the_twins_and_writes_no_speakers_file(tmp_path):
    f = build(tmp_path)
    assert run(f) == [(0, "SPEAKER_01"), (100, "You"), (3000, "SPEAKER_01"), (6000, "SPEAKER_01")]
    assert not f.speakers_path.exists() and f.is_done


def test_finalize_names_keep_two_similar_voices_apart_and_name_the_final_speakers(tmp_path):
    f = build(tmp_path, [sp("SPEAKER_01", "Ana"), ln("system", 3000, "Ben", 5000)])
    got = run(f)
    assert got == [(0, "SPEAKER_01"), (100, "You"), (3000, "SPEAKER_02"), (6000, "SPEAKER_01")]
    data = json.loads(f.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"] == {"SPEAKER_01": "Ana", "SPEAKER_02": "Ben"}
    assert data["from_tags"] is True and data["conflicts"] == []


def test_finalize_names_the_mic_and_reports_a_conflict(tmp_path):
    f = build(tmp_path, [sp("You", "Cy"), ln("system", 0, "Ana", 2000), ln("system", 6000, "Ben", 8000)])
    # Ana and Ben are 0.1 apart in angle (twins): rules keep them apart; C's twin voice goes with one.
    run(f)
    data = json.loads(f.speakers_path.read_text(encoding="utf-8"))
    assert data["speakers"]["You"] == "Cy"
    assert set(data["speakers"].values()) >= {"Cy", "Ana", "Ben"}


def test_finalize_reads_tags_again_for_the_mapping(tmp_path, monkeypatch):
    """A name added while the pass was running names the final speaker (no rule, only the mapping)."""
    f = build(tmp_path, [sp("SPEAKER_01", "Ana")])
    import huddle_helper.finalize as fin

    real = fin.write_names

    def late(folder, live, final):
        mt.append_jsonl(folder.tags_path, sp("You", "Cy"))
        return real(folder, live, final)

    monkeypatch.setattr(fin, "write_names", late)
    run(f)
    assert f.read_speakers() == {"SPEAKER_01": "Ana", "You": "Cy"}


def test_finalize_end_order_final_then_speakers_then_meta_then_done(tmp_path, monkeypatch):
    f = build(tmp_path, [sp("SPEAKER_01", "Ana")])
    order = []
    real_merge, real_update, real_done = mt.MeetingFolder.merge_speakers, mt.MeetingFolder.update_meta, mt.MeetingFolder.mark_done

    def merge(self, *a, **k):
        order.append(("speakers", self.final_path.exists(), self.is_done))
        return real_merge(self, *a, **k)

    def update(self, **ch):
        if ch.get("status") == mt.STATUS_COMPLETE:
            order.append(("meta", self.speakers_path.exists(), self.is_done))
        return real_update(self, **ch)

    def done(self):
        order.append(("done", self.speakers_path.exists(), self.read_meta()["status"]))
        return real_done(self)

    monkeypatch.setattr(mt.MeetingFolder, "merge_speakers", merge)
    monkeypatch.setattr(mt.MeetingFolder, "update_meta", update)
    monkeypatch.setattr(mt.MeetingFolder, "mark_done", done)
    run(f)
    assert order == [("speakers", True, False), ("meta", True, False), ("done", True, "complete")]


def test_no_tags_file_gives_byte_identical_output(tmp_path):
    """The hard requirement: nobody named anyone, so the final file is what it was before WP8. An
    empty tags file, or tags that name nobody, must not change a byte either."""
    plain = build(tmp_path / "a", name="Same")
    run(plain)
    empty = build(tmp_path / "b", name="Same")
    empty.tags_path.write_text("", encoding="utf-8")
    run(empty)
    untagged = build(tmp_path / "c", [sp("SPEAKER_01", "Ana"), tags.tag_speaker_event("SPEAKER_01", None)], name="Same")
    run(untagged)
    def unversioned(folder):  # every final pass stamps its own transcript version (#41); all else is equal
        return re.sub(rb',"transcript_version":"[0-9a-f]+"', b"", folder.final_path.read_bytes())

    ref = unversioned(plain)
    assert b"transcript_version" not in ref
    assert unversioned(empty) == ref and unversioned(untagged) == ref
    assert not plain.speakers_path.exists() and not empty.speakers_path.exists() and not untagged.speakers_path.exists()
    # and the clustering itself is the plain call when no names are passed
    assert label_voices([vec(0), vec(0.1)], [3.0, 3.0], 0.7) == label_voices([vec(0), vec(0.1)], [3.0, 3.0], 0.7, [None, None])


def test_crash_recovery_takes_the_same_path_and_keeps_the_names(tmp_path):
    from huddle_helper import housekeeping

    f = build(tmp_path, [sp("SPEAKER_01", "Ana"), ln("system", 3000, "Ben", 5000)], name="Crash")
    f.update_meta(status=mt.STATUS_RECORDING, clean_exit=None)  # the app died while recording
    done = housekeeping.recover(tmp_path, lambda folder: finalize_meeting(
        folder, NoTurns, keep_audio=False, embedder_factory=AmpEmbedder))
    assert done == [f.meeting_id] and f.is_done
    assert f.read_meta()["status"] == mt.STATUS_INTERRUPTED or f.read_meta()["status"] == mt.STATUS_COMPLETE
    assert f.read_speakers() == {"SPEAKER_01": "Ana", "SPEAKER_02": "Ben"}


def test_a_named_small_cluster_folds_only_into_a_cluster_with_the_same_name():
    from huddle_helper.diarize import fold_small_clusters

    big_a = [vec(0.0), vec(0.02)]
    big_b = [vec(1.2), vec(1.22)]
    stray = [vec(0.1)]  # 1 s, close to A
    vecs, w = big_a + big_b + stray, [20.0, 20.0, 20.0, 20.0, 1.0]
    labels = [0, 0, 1, 1, 2]
    # unnamed: folds into A as always
    assert fold_small_clusters(labels, vecs, w)[4] == 0
    # named Ben, A also Ben: still folds into A (naming a speaker must not leave its fragments loose)
    out = fold_small_clusters(labels, vecs, w, protected={0, 2}, cluster_names={0: "Ben", 2: "Ben"})
    assert out[4] == 0
    # named Ben, A is Ana (B unnamed): stays alone, never handed to the wrong person
    out = fold_small_clusters(labels, vecs, w, protected={0, 2}, cluster_names={0: "Ana", 2: "Ben"})
    assert out[4] == 2
    # named Ben and nobody else is named Ben: never folded (the plan's rule)
    assert fold_small_clusters(labels, vecs, w, protected={2})[4] == 2


def test_map_final_speakers_one_name_names_one_speaker_per_stream():
    """WP11: 4 s of pieces the gate left out must not name a second, larger speaker."""
    ranges = [tags.TaggedRange("system", 0, 60_000, "Ana", tags.SPEAKER),
              tags.TaggedRange("system", 100_000, 104_000, "Ana", tags.SPEAKER),
              tags.TaggedRange("mic", 0, 5_000, "Ana", tags.LINE)]
    final = [seg("system", 0, 60_000, "SPEAKER_03"), seg("system", 100_000, 400_000, "SPEAKER_01"),
             seg("mic", 0, 5_000, "You")]
    mapping, conflicts = tags.map_final_speakers(final, ranges)
    assert mapping == {"SPEAKER_03": "Ana", "You": "Ana"} and conflicts == []
