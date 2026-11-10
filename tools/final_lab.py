"""Dev tool for tuning the final pass on a *copy* of a meeting folder (never the original).

    python tools/final_lab.py embed lab/src/<meeting> [--out lab/cache]
    python tools/final_lab.py sweep lab/cache/<meeting>.json --truth tests/data/truth/test-3.json
    python tools/final_lab.py diff lab/baseline/a/transcript.final.jsonl other/transcript.final.jsonl
    python tools/final_lab.py finalize lab/src/<meeting> --out lab/baseline/<name>

Nothing here writes inside the meeting folder it is given; outputs go under ``lab\\``.
``embed`` runs the production steps up to ``embed_pieces`` and caches vectors, weights and
piece positions (no transcript text). ``sweep`` re-clusters the cache at several thresholds
and checks the groups in a truth file (letters and (source, start_ms) pairs only).
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
from huddle_helper.diarize import SherpaEmbedder, SherpaOfflineDiarizer, cluster_voices, fold_small_clusters
from huddle_helper.finalize import embed_pieces, load_words, split_pieces
from huddle_helper.segments import Segment
from huddle_helper.writer import read_events

DEFAULT_THRESHOLDS = "0.60,0.65,0.70,0.75,0.80"


# -- embed ---------------------------------------------------------------
def cmd_embed(args) -> int:
    folder = mt.MeetingFolder(Path(args.folder))
    settings = Settings()
    meta = folder.read_meta()
    seg_events = [e for e in read_events(folder.live_path) if e.get("type") == "segment"]
    segments = [Segment.from_event(e) for e in seg_events]
    rec = meta.get("settings", {})
    diarized = {src for src, on in (("system", rec.get("diarize_system", True)),
                                    ("mic", rec.get("diarize_mic", False))) if on}
    words = load_words(folder.words_path)

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
        diarizer = diarizer or SherpaOfflineDiarizer(threshold=settings.final_cluster_threshold)
        turns_by_source[src] = diarizer.diarize(audio[src])
        timings[f"diarize_{src}"] = time.perf_counter() - t0
    if not audio:
        print("nothing to embed")
        return 1

    embedder = SherpaEmbedder()

    def embed(source: str, start_ms: int, end_ms: int):
        a = audio.get(source)
        if a is None:
            return None
        return embedder.embed(a[start_ms * SAMPLE_RATE // 1000:end_ms * SAMPLE_RATE // 1000])

    _passthrough, pieces_by_source = split_pieces(segments, words, turns_by_source, diarized, True)
    arrays: dict[str, np.ndarray] = {}
    info: dict[str, list[dict]] = {}
    for src, pieces in pieces_by_source.items():
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
    meta_out = {"meeting": folder.meeting_id, "cluster_threshold": settings.final_cluster_threshold,
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
    """Strict checks: a 'together' group sits in one cluster; 'apart' groups share no cluster."""
    results = []
    for g in truth.get("together", []):
        results.append((f"together {g}", len(dists.get(g, {})) == 1))
    for a, b in truth.get("apart", []):
        shared = set(dists.get(a, {})) & set(dists.get(b, {}))
        results.append((f"apart {a}/{b}", bool(dists.get(a)) and bool(dists.get(b)) and not shared))
    return results


def cmd_sweep(args) -> int:
    info, arrays = load_cache(args.cache)
    source = args.source
    vecs, weights = arrays[f"{source}_vecs"], arrays[f"{source}_weights"]
    pieces = info["pieces"][source]
    truth = json.loads(Path(args.truth).read_text(encoding="utf-8")) if args.truth else None
    groups: dict[str, list[dict]] = {}
    if truth:
        for g, items in truth["groups"].items():
            groups[g], missing = resolve_group([i for i in items if i[0] == source], pieces)
            no_vec = sum(1 for p in groups[g] if p["vec"] < 0)
            notes = ([f"{missing} not found"] if missing else []) + (
                [f"{no_vec} too short to fingerprint"] if no_vec else [])
            print(f"group {g}: {len(items)} lines -> {len(groups[g])} pieces" + "".join(f", {n}" for n in notes))
    vec_list, w_list = list(vecs), [float(w) for w in weights]
    speech = sum(w_list)
    limit = max(4.0, 0.03 * speech)
    print(f"\n{info['meeting']} [{source}]: {len(w_list)} fingerprints, {speech:.0f} s of speech, "
          f"fold limit {limit:.1f} s")
    for g, ps in groups.items():
        print(f"  group {g}: {sum(float(weights[p['vec']]) for p in ps if p['vec'] >= 0):.1f} s")
    for thr in [float(t) for t in args.thresholds.split(",")]:
        raw = cluster_voices(vec_list, w_list, thr)
        for name, labels in (("fold on", fold_small_clusters(raw, vec_list, w_list)), ("fold off", raw)):
            totals: dict[int, float] = {}
            for lab, w in zip(labels, w_list):
                totals[lab] = totals.get(lab, 0.0) + w
            sizes = " ".join(f"{v:.0f}" for v in sorted(totals.values(), reverse=True))
            print(f"\nthreshold {thr:.2f}, {name}: {len(totals)} clusters; seconds per cluster: {sizes}")
            dists = {g: group_clusters(ps, labels, weights) for g, ps in groups.items()}
            for g, d in dists.items():
                print(f"   {g}: {fmt_dist(d)}")
            for what, ok in check_truth(truth, dists) if truth else []:
                print(f"   {what}: {'ok' if ok else 'FAIL'}")
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
    s = Settings()
    count = finalize_meeting(folder, lambda: SherpaOfflineDiarizer(threshold=s.final_cluster_threshold),
                             keep_audio=True, embedder_factory=SherpaEmbedder,
                             voice_threshold=s.final_voice_threshold)
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
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("sweep", help="cluster the cache at several thresholds and check a truth file")
    p.add_argument("cache", help="the .json or .npz written by embed")
    p.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    p.add_argument("--truth")
    p.add_argument("--source", default="system")
    p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("diff", help="compare two transcript.final.jsonl files")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--text", action="store_true", help="also print the text of changed lines")
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("finalize", help="run the real final pass on a copy of the folder")
    p.add_argument("folder")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_finalize)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
