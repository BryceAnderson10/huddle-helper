"""Dev tool for tuning the final pass on a *copy* of a meeting folder (never the original).

    python tools/final_lab.py embed lab/src/<meeting> [--out lab/cache]
    python tools/final_lab.py sweep lab/cache/<meeting>.json --truth tests/data/truth/test-3.json
    python tools/final_lab.py matrix --cache lab/cache --truth tests/data/truth --baseline lab/baseline
    python tools/final_lab.py score [--thresholds 0.68,0.70,0.72] [--anchors 3,4]
    python tools/final_lab.py diff lab/baseline/a/transcript.final.jsonl other/transcript.final.jsonl
    python tools/final_lab.py finalize lab/src/<meeting> --out lab/baseline/<name>

Nothing here writes inside the meeting folder it is given; outputs go under ``lab\\``.
``embed`` runs the production steps up to ``embed_pieces`` and caches vectors, weights and
piece positions (no transcript text). ``sweep`` re-clusters the cache at several thresholds
(optionally "anchors first", see ``anchors_first``) and checks the groups in a truth file
(letters and (source, start_ms) pairs only; groups under "unchecked" are listed, never checked).
``matrix`` scores every setting against WP1's acceptance matrix for the three kept recordings.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from huddle_helper import meeting as mt
from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.audio.buffer import read_audio
from huddle_helper.config import Settings
from huddle_helper.diarize import (SherpaEmbedder, SherpaOfflineDiarizer, cluster_voices, fold_small_clusters,
                                   profile)
from huddle_helper.finalize import diarize_stretches, embed_pieces, load_words, outside_stretches, split_pieces
from huddle_helper.segments import Segment
from huddle_helper.writer import read_events

DEFAULT_THRESHOLDS = "0.60,0.65,0.70,0.75,0.80"


def voice_for(stem: str | None):
    """(cutoffs, model file) for ``--embedding STEM``; (the shipped model's cutoffs, None) without it.

    The file is looked for under lab/models (any subfolder). A model with no entry in
    diarize.PROFILES cannot be run through the final pass: add its cutoffs there first."""
    if not stem:
        return profile(), None
    path = next((Path("lab") / "models").rglob(stem + ".onnx"), None)
    if path is None:
        raise SystemExit(f"no {stem}.onnx under lab/models")
    return profile(stem), path


# -- embed ---------------------------------------------------------------
def cmd_embed(args) -> int:
    folder = mt.MeetingFolder(Path(args.folder))
    voice, model_path = voice_for(args.embedding)
    meta = folder.read_meta()
    seg_events = [e for e in read_events(folder.live_path) if e.get("type") == "segment"]
    segments = [Segment.from_event(e) for e in seg_events]
    rec = meta.get("settings", {})
    diarized = {src for src, on in (("system", rec.get("diarize_system", True)),
                                    ("mic", mt.has_shared_mic(meta))) if on}
    words = load_words(folder.words_path)
    mic_stretches = mt.shared_mic_stretches(meta) if "mic" in diarized else None  # as the final pass

    timings: dict[str, float] = {}
    turns_by_source, audio = {}, {}
    diarizer = None
    for src in sorted(diarized):
        if not folder.audio_file(src).exists():
            print(f"no audio for {src}; skipped")
            continue
        t0 = time.perf_counter()
        audio[src] = read_audio(folder.audio_file(src))
        timings["read_audio"] = timings.get("read_audio", 0.0) + time.perf_counter() - t0
        t0 = time.perf_counter()
        diarizer = diarizer or SherpaOfflineDiarizer(threshold=voice.split_cluster, embedding_path=model_path)
        turns_by_source[src] = (diarize_stretches(diarizer, audio[src], mic_stretches) if src == "mic"
                                else diarizer.diarize(audio[src]))
        timings[f"diarize_{src}"] = time.perf_counter() - t0
    if not audio:
        print("nothing to embed")
        return 1

    embedder = SherpaEmbedder(model_path=model_path)

    def embed(source: str, start_ms: int, end_ms: int):
        a = audio.get(source)
        if a is None:
            return None
        return embedder.embed(a[start_ms * SAMPLE_RATE // 1000:end_ms * SAMPLE_RATE // 1000])

    _passthrough, pieces_by_source = split_pieces(segments, words, turns_by_source, diarized, True,
                                                  mic_stretches)
    arrays: dict[str, np.ndarray] = {}
    info: dict[str, list[dict]] = {}
    for src, pieces in pieces_by_source.items():
        you = [(line, seg) for line, seg in pieces if outside_stretches(seg, mic_stretches)]
        if you:  # the user's own voice, the final pass's reference for You inside the stretches (#56)
            ref_vecs, ref_weights, _ = embed_pieces(you, embed)
            arrays[f"{src}_you_vecs"] = (np.stack(ref_vecs).astype(np.float32) if ref_vecs
                                         else np.zeros((0, 0), np.float32))
            arrays[f"{src}_you_weights"] = np.asarray(ref_weights, dtype=np.float64)
        pieces = [(line, seg) for line, seg in pieces if not outside_stretches(seg, mic_stretches)]  # You
        t0 = time.perf_counter()
        vecs, weights, idx = embed_pieces(pieces, embed)
        timings["embed_pieces"] = timings.get("embed_pieces", 0.0) + time.perf_counter() - t0
        arrays[f"{src}_vecs"] = np.stack(vecs).astype(np.float32) if vecs else np.zeros((0, 0), np.float32)
        arrays[f"{src}_weights"] = np.asarray(weights, dtype=np.float64)
        arrays[f"{src}_idx"] = np.asarray(idx, dtype=np.int64)
        vec_of = {piece: k for k, piece in enumerate(idx)}
        info[src] = []
        for i, (line, seg) in enumerate(pieces):
            info[src].append({"piece": i, "line": line, "source": src, "start_ms": seg.start_ms,
                              "end_ms": seg.end_ms, "duration": round((seg.end_ms - seg.start_ms) / 1000, 3),
                              "live_seq": seg_events[line].get("seq"), "live_speaker": segments[line].speaker,
                              "vec": vec_of.get(i, -1)})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / folder.meeting_id
    np.savez(stem.with_suffix(".npz"), **arrays)
    meta_out = {"meeting": folder.meeting_id, "cluster_threshold": voice.split_cluster, "model": voice.model,
                "audio_s": round(max(len(a) for a in audio.values()) / SAMPLE_RATE, 2),
                "timings_s": {k: round(v, 2) for k, v in timings.items()}, "pieces": info}
    stem.with_suffix(".json").write_text(json.dumps(meta_out, indent=1), encoding="utf-8")
    with_vec = sum(len(a) for k, a in arrays.items() if k.endswith("_idx"))
    print(f"cached {sum(len(v) for v in info.values())} pieces ({with_vec} with vectors) -> {stem}.json/.npz")
    print("timings (s):", meta_out["timings_s"])
    return 0


# -- sweep ---------------------------------------------------------------
def load_cache(path: str | Path) -> tuple[dict, dict[str, np.ndarray]]:
    p = Path(path)
    info = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
    return info, dict(np.load(p.with_suffix(".npz")))


def resolve_group(items: list, pieces: list[dict], tolerance_ms: int = 1500) -> tuple[list[dict], int]:
    """Pieces for a truth group's (source, start_ms) items. Returns (pieces, unresolved count)."""
    found: dict[int, dict] = {}
    missing = 0
    for source, start in items:
        pool = [p for p in pieces if p["source"] == source]
        hit = next((p for p in pool if p["start_ms"] <= start < p["end_ms"]), None)
        if hit is None and pool:
            near = min(pool, key=lambda p: abs(p["start_ms"] - start))
            hit = near if abs(near["start_ms"] - start) <= tolerance_ms else None
        if hit is None:
            missing += 1
        else:
            found[hit["piece"]] = hit
    return list(found.values()), missing


def group_clusters(group: list[dict], labels: list[int], weights: np.ndarray) -> dict[int, float]:
    """Seconds of the group's fingerprinted pieces per cluster."""
    out: dict[int, float] = {}
    for p in group:
        if p["vec"] >= 0:
            out[labels[p["vec"]]] = out.get(labels[p["vec"]], 0.0) + float(weights[p["vec"]])
    return out


def fmt_dist(dist: dict[int, float]) -> str:
    return ", ".join(f"c{k} {v:.0f}s" for k, v in sorted(dist.items(), key=lambda kv: -kv[1])) or "-"


def check_truth(truth: dict, dists: dict[str, dict[int, float]]) -> list[tuple[str, bool]]:
    """Strict checks: a 'together' group sits in one cluster; 'apart' groups share no cluster.

    Groups listed under "unchecked" (nobody has confirmed them) are never part of a check.
    """
    results = []
    skip = set(truth.get("unchecked", []))
    for g in [g for g in truth.get("together", []) if g not in skip]:
        results.append((f"together {g}", len(dists.get(g, {})) == 1))
    for a, b in [(a, b) for a, b in truth.get("apart", []) if a not in skip and b not in skip]:
        shared = set(dists.get(a, {})) & set(dists.get(b, {}))
        results.append((f"apart {a}/{b}", bool(dists.get(a)) and bool(dists.get(b)) and not shared))
    return results


def anchors_first(vecs: list[np.ndarray], weights: list[float], starts: list[int], threshold: float,
                  anchor_s: float, assign: float) -> list[int]:
    """Lab option (WP1 (b)): cluster only pieces of ``anchor_s`` seconds or more, then place the rest.

    Weighted centroids of the anchor clusters take each shorter piece if it is at least ``assign``
    like the nearest one; any other piece takes the label of the nearest labelled piece in time
    (the neighbour rule). With no anchor, falls back to plain clustering.
    """
    anchors = [i for i, w in enumerate(weights) if w >= anchor_s]
    if not anchors:
        return cluster_voices(vecs, weights, threshold)
    a_labels = cluster_voices([vecs[i] for i in anchors], [weights[i] for i in anchors], threshold)
    labels: dict[int, int] = dict(zip(anchors, a_labels))
    cents = {}
    for k in set(a_labels):
        c = np.sum([vecs[i] * weights[i] for i in anchors if labels[i] == k], axis=0)
        cents[k] = c / (np.linalg.norm(c) or 1.0)
    rest = []
    for i in range(len(vecs)):
        if i in labels:
            continue
        k, sim = max(((k, float(np.dot(vecs[i], c))) for k, c in cents.items()), key=lambda kv: kv[1])
        if sim >= assign:
            labels[i] = k
        else:
            rest.append(i)
    placed = dict(labels)  # the neighbour rule looks at anchors and assigned pieces only
    for i in rest:
        near = min(placed, key=lambda j: abs(starts[j] - starts[i]))
        labels[i] = placed[near]
    return [labels[i] for i in range(len(vecs))]


def vec_starts(pieces: list[dict], n: int) -> list[int]:
    out = [0] * n
    for p in pieces:
        if p["vec"] >= 0:
            out[p["vec"]] = p["start_ms"]
    return out


def seconds_per_cluster(labels: list[int], weights: list[float]) -> dict[int, float]:
    totals: dict[int, float] = {}
    for lab, w in zip(labels, weights):
        totals[lab] = totals.get(lab, 0.0) + w
    return totals


def cmd_sweep(args) -> int:
    info, arrays = load_cache(args.cache)
    source = args.source
    vecs, weights = arrays[f"{source}_vecs"], arrays[f"{source}_weights"]
    pieces = info["pieces"][source]
    truth = json.loads(Path(args.truth).read_text(encoding="utf-8")) if args.truth else None
    unchecked = set((truth or {}).get("unchecked", []))
    groups: dict[str, list[dict]] = {}
    if truth:
        for g, items in truth["groups"].items():
            groups[g], missing = resolve_group([i for i in items if i[0] == source], pieces)
            no_vec = sum(1 for p in groups[g] if p["vec"] < 0)
            notes = ([f"{missing} not found"] if missing else []) + (
                [f"{no_vec} too short to fingerprint"] if no_vec else []) + (
                ["unchecked: reported, not part of any check"] if g in unchecked else [])
            print(f"group {g}: {len(items)} lines -> {len(groups[g])} pieces" + "".join(f", {n}" for n in notes))
    vec_list, w_list = list(vecs), [float(w) for w in weights]
    starts = vec_starts(pieces, len(vec_list))
    speech = sum(w_list)
    limit = max(4.0, 0.03 * speech)
    print(f"\n{info['meeting']} [{source}]: {len(w_list)} fingerprints, {speech:.0f} s of speech, "
          f"fold limit {limit:.1f} s")
    for g, ps in groups.items():
        print(f"  group {g}: {sum(float(weights[p['vec']]) for p in ps if p['vec'] >= 0):.1f} s")
    settings: list[tuple[str, list[int]]] = []
    for thr in [float(t) for t in args.thresholds.split(",")]:
        raw = cluster_voices(vec_list, w_list, thr)
        settings.append((f"threshold {thr:.2f}, gated fold", fold_small_clusters(raw, vec_list, w_list)))
        if args.fold_off:
            settings.append((f"threshold {thr:.2f}, fold off", raw))
        for anchor_s in [float(x) for x in args.anchors.split(",") if x]:
            for assign in [float(x) for x in args.assign.split(",")]:
                raw = anchors_first(vec_list, w_list, starts, thr, anchor_s, assign)
                settings.append((f"threshold {thr:.2f}, anchors {anchor_s:g} s assign {assign:.2f}, gated fold",
                                 fold_small_clusters(raw, vec_list, w_list)))
    for name, labels in settings:
        totals = seconds_per_cluster(labels, w_list)
        sizes = " ".join(f"{v:.0f}" for v in sorted(totals.values(), reverse=True))
        print(f"\n{name}: {len(totals)} clusters; seconds per cluster: {sizes}")
        dists = {g: group_clusters(ps, labels, weights) for g, ps in groups.items()}
        for g, d in dists.items():
            print(f"   {g}{' (unchecked)' if g in unchecked else ''}: {fmt_dist(d)}")
        for what, ok in check_truth(truth, dists) if truth else []:
            print(f"   {what}: {'ok' if ok else 'FAIL'}")
    return 0


# -- acceptance matrix ---------------------------------------------------
RECORDINGS = {"test-2": "*test-2-2", "test-3": "*test-3", "test-4": "*_1605_*"}
S_MIN_SIMILARITY = 0.70


def baseline_speakers(pieces: list[dict], n: int, baseline: Path, source: str = "system") -> list[str | None]:
    """Baseline final speaker of each fingerprint's piece (the line holding the piece's midpoint)."""
    segs = [s for s in read_segments(baseline) if s["source"] == source]
    out: list[str | None] = [None] * n
    for p in pieces:
        if p["vec"] < 0:
            continue
        mid = (p["start_ms"] + p["end_ms"]) // 2
        hit = next((s for s in segs if s["start_ms"] <= mid < s["end_ms"]), None)
        if hit is None and segs:
            hit = max(segs, key=lambda s: min(s["end_ms"], p["end_ms"]) - max(s["start_ms"], p["start_ms"]))
        out[p["vec"]] = hit["speaker"] if hit else None
    return out


def majority(items: list) -> object | None:
    return max(set(items), key=items.count) if items else None


def matrix_rows(name: str, labels: list[int], vecs: list[np.ndarray], weights: list[float], pieces: list[dict],
                truth: dict | None, base: list[str | None]) -> list[tuple[str, bool]]:
    """Acceptance checks of WP1 for one recording and one clustering. Rows: (name, passed).

    Rows named "info ..." come from the truth file but are not part of the acceptance matrix.
    """
    wts = np.asarray(weights)
    rows: list[tuple[str, bool]] = []
    groups = {g: resolve_group([i for i in items if i[0] == "system"], pieces)[0]
              for g, items in (truth or {}).get("groups", {}).items()}
    dists = {g: group_clusters(ps, labels, wts) for g, ps in groups.items()}
    if name == "test-3":
        rows += [(w.replace("together ", "").replace("apart ", "") + (" one" if w.startswith("together") else ""), ok)
                 for w, ok in check_truth(truth, dists)]
        big = [k for k, v in seconds_per_cluster(labels, weights).items() if v >= 4.0]
        rows.append(("3 clusters >= 4 s", len(big) == 3))
    elif name == "test-4":
        checks = dict(check_truth(truth, dists))
        rows += [("M one", checks["together M"]), ("M/L apart", checks["apart M/L"]),
                 ("M/J apart", checks["apart M/J"])]
        j_labels = [labels[p["vec"]] for p in groups["J"] if p["vec"] >= 0]
        j_base = [base[p["vec"]] for p in groups["J"] if p["vec"] >= 0]
        j_cluster, j_spk = majority(j_labels), majority([b for b in j_base if b])
        new = {i for i, lab in enumerate(labels) if lab == j_cluster}
        old = {i for i, b in enumerate(base) if b == j_spk}
        rows.append(("J exact", new == old))
        s_idx = {p["vec"] for p in groups["S"] if p["vec"] >= 0}
        s_ok = True  # judged on the cluster holding most of S's speech (a stray short piece may fold)
        k = majority([labels[i] for i in s_idx for _ in range(int(weights[i] * 10))])
        mine = [i for i in s_idx if labels[i] == k]
        rest = [i for i, lab in enumerate(labels) if lab == k and i not in s_idx]
        if mine and rest:
            cs = np.sum([vecs[i] * weights[i] for i in mine], axis=0)
            cr = np.sum([vecs[i] * weights[i] for i in rest], axis=0)
            sim = float(np.dot(cs, cr) / ((np.linalg.norm(cs) * np.linalg.norm(cr)) or 1.0))
            s_ok = sim >= S_MIN_SIMILARITY
        rows.append(("S not folded below 0.70", s_ok))
        for w in ("together J", "together L", "apart J/L"):
            rows.append(("info " + w, checks[w]))
    else:  # test-2: the two main speakers' partition for every line of 3 s or more
        main = sorted({b for b in base if b}, key=lambda b: -sum(w for w, x in zip(weights, base) if x == b))[:2]
        keep = [i for i, b in enumerate(base) if b in main and weights[i] >= 3.0]
        pairs = {(base[i], labels[i]) for i in keep}
        same = len({b for b, _ in pairs}) == len(pairs) == len({lab for _, lab in pairs})
        rows.append(("main speakers equal baseline (>= 3 s)", same))
    return rows


def cmd_matrix(args) -> int:
    from itertools import product

    cache_dir, truth_dir, base_dir = Path(args.cache), Path(args.truth), Path(args.baseline)
    ctx = {}
    for name, pattern in RECORDINGS.items():
        path = next(cache_dir.glob(pattern + ".json"))
        info, arrays = load_cache(path)
        pieces = info["pieces"]["system"]
        v, w = list(arrays["system_vecs"]), [float(x) for x in arrays["system_weights"]]
        tpath = truth_dir / f"{name}.json"
        truth = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
        base = baseline_speakers(pieces, len(v), base_dir / name / "transcript.final.jsonl")
        ctx[name] = (v, w, pieces, truth, base, vec_starts(pieces, len(v)))
    settings: list[tuple[str, dict[str, list[int]]]] = []

    def run(make) -> dict[str, list[int]]:
        return {n: fold_small_clusters(make(c[0], c[1], c[5]), c[0], c[1]) for n, c in ctx.items()}

    if args.mode in ("plain", "both"):
        for t in [float(x) for x in args.thresholds.split(",")]:
            settings.append((f"plain T={t:.3f}", run(lambda v, w, st, t=t: cluster_voices(v, w, t))))
    if args.mode in ("anchors", "both"):
        for a, t, g in product([float(x) for x in args.anchors.split(",")],
                               [float(x) for x in args.anchor_t.split(",")],
                               [float(x) for x in args.assign.split(",")]):
            settings.append((f"anchors a={a:g} T={t:.2f} assign={g:.2f}",
                             run(lambda v, w, st, a=a, t=t, g=g: anchors_first(v, w, st, t, a, g))))
    print("| setting | " + " | ".join(ctx) + " | all |")
    print("| --- | " + " | ".join("---" for _ in ctx) + " | --- |")
    for name, by_rec in settings:
        cells, ok_all = [], True
        for rec, (v, w, pieces, truth, base, _st) in ctx.items():
            rows = matrix_rows(rec, by_rec[rec], v, w, pieces, truth, base)
            fails = [n for n, ok in rows if not ok and not n.startswith("info ")]
            ok_all = ok_all and not fails
            cells.append("pass" if not fails else "FAIL: " + ", ".join(fails))
        print(f"| {name} | " + " | ".join(cells) + f" | {'PASS' if ok_all else 'fail'} |")
        if args.detail:
            for rec, (v, w, pieces, truth, base, _st) in ctx.items():
                tot = sorted(seconds_per_cluster(by_rec[rec], w).values(), reverse=True)
                rows = matrix_rows(rec, by_rec[rec], v, w, pieces, truth, base)
                print(f"    {rec}: clusters (s) " + " ".join(f"{x:.0f}" for x in tot) + "; "
                      + ", ".join(f"{n} {'ok' if ok else 'FAIL'}" for n, ok in rows))
    return 0


# -- score ---------------------------------------------------------------
SCORED = {**RECORDINGS, "test-5": "*test-meeting-4"}


def truth_errors(truth: dict, pieces: list[dict], labels: list[int], weights) -> tuple[float, float, float]:
    """Seconds of confirmed speech with the wrong speaker: (split, merged, total).

    Split: a group's seconds outside the cluster that holds most of it. Merged: for two groups
    that must be apart but share that main cluster, the smaller group's seconds. Groups under
    "unchecked" are left out.
    """
    skip = set(truth.get("unchecked", []))
    groups = {g: [p["vec"] for p in resolve_group(items, pieces)[0] if p["vec"] >= 0]
              for g, items in truth.get("groups", {}).items() if g not in skip}
    checked = set(truth.get("together", [])) | {g for pair in truth.get("apart", []) for g in pair}
    main: dict[str, int] = {}
    size: dict[str, float] = {}
    split = 0.0
    for g, idx in groups.items():
        if g not in checked or not idx:
            continue
        dist: dict[int, float] = {}
        for i in idx:
            dist[labels[i]] = dist.get(labels[i], 0.0) + float(weights[i])
        main[g] = max(dist, key=dist.get)
        size[g] = sum(dist.values())
        if g in truth.get("together", []):
            split += size[g] - dist[main[g]]
    merged = sum(min(size[a], size[b]) for a, b in truth.get("apart", [])
                 if a in main and b in main and main[a] == main[b])
    return split, merged, sum(size.values())


def cmd_score(args) -> int:
    """Wrong-speaker seconds for every recording that has a truth file, plain and anchors-first."""
    cache_dir, truth_dir = Path(args.cache), Path(args.truth)
    ctx = {}
    for name, pattern in SCORED.items():
        tpath = truth_dir / f"{name}.json"
        path = next(cache_dir.glob(pattern + ".json"), None)
        if path is None or not tpath.exists():
            continue
        info, arrays = load_cache(path)
        pieces = info["pieces"]["system"]
        v, w = list(arrays["system_vecs"]), [float(x) for x in arrays["system_weights"]]
        ctx[name] = (v, w, pieces, json.loads(tpath.read_text(encoding="utf-8")), vec_starts(pieces, len(v)))
    modes = [("plain", 0.0)] + [(f"anchors {a:g} s", a) for a in (float(x) for x in args.anchors.split(",") if x)]
    print("| setting | " + " | ".join(f"{n} split/merged" for n in ctx) + " | wrong |")
    print("| --- | " + " | ".join("---" for _ in ctx) + " | --- |")
    for mode, anchor_s in modes:
        for t in [float(x) for x in args.thresholds.split(",")]:
            cells, wrong, total = [], 0.0, 0.0
            for v, w, pieces, truth, starts in ctx.values():
                raw = anchors_first(v, w, starts, t, anchor_s, -1.0) if anchor_s else cluster_voices(v, w, t)
                s, m, tot = truth_errors(truth, pieces, fold_small_clusters(raw, v, w, min_similarity=t), w)
                cells.append(f"{s:.1f} / {m:.1f}")
                wrong, total = wrong + s + m, total + tot
            print(f"| {mode} T={t:.2f} | " + " | ".join(cells) + f" | {wrong:.1f} s of {total:.0f} |")
    return 0


# -- diff ----------------------------------------------------------------
def read_segments(path: str | Path) -> list[dict]:
    return [e for e in read_events(Path(path)) if e.get("type") == "segment"]


def match_segments(a: list[dict], b: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair segments of the same source by largest time overlap (each used once)."""
    cands = []
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            if x["source"] != y["source"]:
                continue
            ov = min(x["end_ms"], y["end_ms"]) - max(x["start_ms"], y["start_ms"])
            if ov > 0:
                cands.append((ov, i, j))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_a, used_b, pairs = set(), set(), []
    for _ov, i, j in cands:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            pairs.append((a[i], b[j]))
    return (sorted(pairs, key=lambda p: p[0]["start_ms"]),
            [x for i, x in enumerate(a) if i not in used_a], [y for j, y in enumerate(b) if j not in used_b])


def moved_pairs(pairs: list[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
    """Pairs whose speaker partition differs, comparing groupings and not label names.

    Per source, each label is mapped to the label it overlaps most on the other side; a
    line is moved when its pair is not that mapping in both directions.
    """
    counts: dict[tuple[str, str, str], int] = {}
    for x, y in pairs:
        key = (x["source"], x["speaker"], y["speaker"])
        counts[key] = counts.get(key, 0) + 1
    best_a: dict[tuple[str, str], tuple[int, str]] = {}
    best_b: dict[tuple[str, str], tuple[int, str]] = {}
    for (src, la, lb), n in sorted(counts.items()):
        if n > best_a.get((src, la), (0, ""))[0]:
            best_a[(src, la)] = (n, lb)
        if n > best_b.get((src, lb), (0, ""))[0]:
            best_b[(src, lb)] = (n, la)
    return [(x, y) for x, y in pairs
            if best_a[(x["source"], x["speaker"])][1] != y["speaker"]
            or best_b[(y["source"], y["speaker"])][1] != x["speaker"]]


def changed_text_or_bounds(pairs: list[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
    return [(x, y) for x, y in pairs
            if (x["start_ms"], x["end_ms"], x["text"]) != (y["start_ms"], y["end_ms"], y["text"])]


def cmd_diff(args) -> int:
    a, b = read_segments(args.a), read_segments(args.b)
    pairs, only_a, only_b = match_segments(a, b)
    moved, changed = moved_pairs(pairs), changed_text_or_bounds(pairs)
    print(f"segments: A {len(a)}, B {len(b)}; matched {len(pairs)}, "
          f"only in A {len(only_a)}, only in B {len(only_b)}")
    print(f"speaker partition changed: {len(moved)} lines; text or boundaries changed: "
          f"{len(changed) + len(only_a) + len(only_b)} lines")
    rows = [(x["start_ms"], x["source"], f"speaker {x['speaker']} -> {y['speaker']}", x, y) for x, y in moved]
    rows += [(x["start_ms"], x["source"], "text/bounds changed" + (
        "" if x["text"] != y["text"] else " (bounds only)"), x, y) for x, y in changed]
    rows += [(x["start_ms"], x["source"], "only in A", x, None) for x in only_a]
    rows += [(y["start_ms"], y["source"], "only in B", None, y) for y in only_b]
    for start, source, what, x, y in sorted(rows, key=lambda r: (r[0], r[1])):
        line = f"{start:>9} ms {source:<6} {what}"
        if x and y and what.startswith("text/bounds"):
            line += f"  [{x['start_ms']}-{x['end_ms']} -> {y['start_ms']}-{y['end_ms']}]"
        print(line)
        if args.text:
            for tag, s in (("A", x), ("B", y)):
                if s:
                    print(f"      {tag}: {s['text']}")
    return 0


# -- finalize ------------------------------------------------------------
def cmd_finalize(args) -> int:
    from huddle_helper.finalize import finalize_meeting

    src, dest = Path(args.folder).resolve(), Path(args.out).resolve()
    if dest == src or src in dest.parents:
        print("refusing: --out is inside the source folder", file=sys.stderr)
        return 2
    if dest.exists():
        print(f"refusing: {dest} already exists", file=sys.stderr)
        return 2
    shutil.copytree(src, dest)
    for name in ("transcript.final.jsonl", "DONE"):
        (dest / name).unlink(missing_ok=True)
    folder = mt.MeetingFolder(dest)
    folder.update_meta_settings(keep_audio=True)  # the copy keeps its audio whatever the meeting chose
    voice, model_path = voice_for(args.embedding)
    n = args.threads  # 0: the old defaults (diarizer 2 threads, embedder 1)
    count = finalize_meeting(
        folder, lambda: SherpaOfflineDiarizer(threshold=voice.split_cluster, num_threads=n or 2,
                                              merge_small=args.merge_small, embedding_path=model_path),
        keep_audio=True, embedder_factory=lambda: SherpaEmbedder(num_threads=n or 1, model_path=model_path),
        voice=voice)
    print(f"{count} segments -> {folder.final_path}")
    print("final_stats:", json.dumps(folder.read_meta().get("final_stats")))
    totals: dict[str, float] = {}
    for seg in read_segments(folder.final_path):
        key = f"{seg['source']}:{seg['speaker']}"
        totals[key] = totals.get(key, 0.0) + (seg["end_ms"] - seg["start_ms"]) / 1000
    print("seconds per speaker:", ", ".join(f"{k} {v:.0f}" for k, v in sorted(totals.items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("embed", help="cache fingerprints of a meeting folder's pieces")
    p.add_argument("folder")
    p.add_argument("--out", default=str(Path("lab") / "cache"))
    p.add_argument("--embedding", help="fingerprint model file stem under lab/models (default: the shipped model)")
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("sweep", help="cluster the cache at several thresholds and check a truth file")
    p.add_argument("cache", help="the .json or .npz written by embed")
    p.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    p.add_argument("--truth")
    p.add_argument("--source", default="system")
    p.add_argument("--fold-off", action="store_true", help="also show each threshold without the fold")
    p.add_argument("--anchors", default="", help="anchors-first piece lengths in seconds, e.g. 2,3,4")
    p.add_argument("--assign", default="0.60", help="anchors-first: similarity needed to take a short piece")
    p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("matrix", help="score settings against WP1's acceptance matrix (Tests #2-#4)")
    p.add_argument("--cache", default=str(Path("lab") / "cache"))
    p.add_argument("--truth", default=str(Path("tests") / "data" / "truth"))
    p.add_argument("--baseline", default=str(Path("lab") / "baseline"))
    p.add_argument("--mode", choices=("plain", "anchors", "both"), default="both")
    p.add_argument("--thresholds", default="0.66,0.68,0.70,0.72,0.74,0.76", help="plain average linkage")
    p.add_argument("--anchors", default="2,3,4")
    p.add_argument("--anchor-t", default="0.68,0.70,0.72,0.74")
    p.add_argument("--assign", default="0.55,0.60,0.65")
    p.add_argument("--detail", action="store_true", help="cluster seconds and every row per recording")
    p.set_defaults(fn=cmd_matrix)
    p = sub.add_parser("score", help="wrong-speaker seconds against every truth file (Tests #3-#5)")
    p.add_argument("--cache", default=str(Path("lab") / "cache"))
    p.add_argument("--truth", default=str(Path("tests") / "data" / "truth"))
    p.add_argument("--thresholds", default="0.64,0.66,0.68,0.70,0.72,0.74,0.76")
    p.add_argument("--anchors", default="3,4", help="anchors-first piece lengths in seconds; empty for none")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("diff", help="compare two transcript.final.jsonl files")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--text", action="store_true", help="also print the text of changed lines")
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("finalize", help="run the real final pass on a copy of the folder")
    p.add_argument("folder")
    p.add_argument("--out", required=True)
    p.add_argument("--merge-small", action=argparse.BooleanOptionalAction, default=True,
                   help="sherpa diarizer's merge_small step (default on)")
    p.add_argument("--threads", type=int, default=0, help="threads for embedder and diarizer; 0 = old defaults")
    p.add_argument("--embedding", help="fingerprint model file stem under lab/models (default: the shipped model)")
    p.set_defaults(fn=cmd_finalize)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
