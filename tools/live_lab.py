"""Dev tool: replay the live speaker labelling over kept recordings with any fingerprint model.

    python tools/live_lab.py embed [MODEL_STEM ...]         # per-line fingerprints -> lab/cache_lines/<model>/
    python tools/live_lab.py replay MODEL_STEM --final 0.43 [--match 0.40,0.45,0.50] [--adopt 0.30,0.35]

``embed`` fingerprints every live system line (its start to its end in transcript.jsonl; the app
embeds the whole utterance, which also holds the padding around it, so this is close, not exact).
Models are the .onnx files in lab/models/speaker and lab/models/old, plus the shipped one. ``replay`` runs
``OnlineClusterer`` over those lines in time order and compares the live labels with (a) the truth
files and (b) the speakers that ``cluster_voices`` finds from the same lines at ``--final``.
Nothing is written inside a meeting folder.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import final_lab as fl  # noqa: E402
from huddle_helper import models  # noqa: E402
from huddle_helper.audio import SAMPLE_RATE  # noqa: E402
from huddle_helper.audio.buffer import read_audio  # noqa: E402
from huddle_helper.diarize import (MIN_FINGERPRINT_S, SYSTEM_PREFIX, OnlineClusterer, cluster_voices,  # noqa: E402
                                   fold_small_clusters)
from huddle_helper.finalize import load_words  # noqa: E402
from huddle_helper.writer import read_events  # noqa: E402

SRC, OUT, MODELS = ROOT / "lab" / "src", ROOT / "lab" / "cache_lines", ROOT / "lab" / "models" / "speaker"
RECS = {"test-2": "*test-2-2", "test-3": "*test-3", "test-4": "*_1605_*", "test-5": "*test-meeting-4"}


def model_files() -> dict[str, Path]:
    found = {p.stem: p for d in (MODELS, MODELS.parent / "old") for p in sorted(d.glob("*.onnx"))}
    shipped = models.embedding_model()
    if shipped.exists():
        found.setdefault(shipped.stem, shipped)
    return found


def cmd_embed(args) -> int:
    import sherpa_onnx

    files = model_files()
    for stem in args.models or list(files):
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(files[stem]), num_threads=1))
        for folder in sorted(p for p in SRC.iterdir() if (p / "audio" / "system.flac").exists()):
            dest = OUT / stem / (folder.name + ".npz")
            if dest.exists() and not args.force:
                continue
            audio = read_audio(folder / "audio" / "system.flac")
            segs = [e for e in read_events(folder / "transcript.jsonl")
                    if e.get("type") == "segment" and e.get("source") == "system"]
            words = load_words(folder / "audio" / "words.jsonl")
            vecs, rows, secs = [], [], []
            for e in segs:
                clip = audio[e["start_ms"] * SAMPLE_RATE // 1000:e["end_ms"] * SAMPLE_RATE // 1000].astype(np.float32)
                t0 = time.perf_counter()
                v = None
                if len(clip) >= MIN_FINGERPRINT_S * SAMPLE_RATE:  # as SherpaEmbedder: shorter has no fingerprint
                    st = ex.create_stream()
                    st.accept_waveform(SAMPLE_RATE, clip)
                    st.input_finished()
                    if ex.is_ready(st):
                        v = np.asarray(ex.compute(st), dtype=np.float32)
                        v = v / np.linalg.norm(v) if np.linalg.norm(v) else None
                secs.append(time.perf_counter() - t0)
                w = words.get((e["source"], int(e["start_ms"])), [])
                speech = (w[-1].end_ms - w[0].start_ms) / 1000 if w else (e["end_ms"] - e["start_ms"]) / 1000
                vecs.append(v if v is not None else np.zeros(ex.dim, np.float32))
                rows.append((e["start_ms"], e["end_ms"], speech, 0.0 if v is None else 1.0))
            dest.parent.mkdir(parents=True, exist_ok=True)
            np.savez(dest, vecs=np.stack(vecs), rows=np.asarray(rows, dtype=np.float64),
                     embed_s=np.asarray(secs, dtype=np.float64))
            print(f"{stem} | {folder.name}: {len(rows)} lines, median {1000 * np.median(secs):.0f} ms, "
                  f"p90 {1000 * np.percentile(secs, 90):.0f} ms per line (1 thread)", flush=True)
    return 0


def load_lines(stem: str) -> dict[str, dict]:
    out = {}
    for name, pat in RECS.items():
        path = next((OUT / stem).glob(pat + ".npz"), None)
        if path is None:
            continue
        d = np.load(path)
        tp = ROOT / "tests" / "data" / "truth" / f"{name}.json"
        out[name] = {"vecs": d["vecs"].astype(np.float64), "rows": d["rows"], "embed_s": d["embed_s"],
                     "truth": json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None}
    return out


def live_labels(rec: dict, match: float, adopt: float, min_new_s: float = 2.0) -> list[int]:
    cl = OnlineClusterer(SYSTEM_PREFIX, match, min_new_s, adopt_threshold=adopt)
    out = []
    for v, (_s, _e, speech, has) in zip(rec["vecs"], rec["rows"]):
        out.append(int(cl.assign(v if has else None, float(speech))[len(SYSTEM_PREFIX):]))
    return out


def final_labels(rec: dict, threshold: float) -> list[int]:
    """Speakers from the same lines by the final pass's method; lines without a fingerprint get -1."""
    idx = [i for i, r in enumerate(rec["rows"]) if r[3]]
    v = [rec["vecs"][i] for i in idx]
    w = [float(rec["rows"][i][1] - rec["rows"][i][0]) / 1000 for i in idx]
    lab = fold_small_clusters(cluster_voices(v, w, threshold), v, w, min_similarity=threshold)
    out = [-1] * len(rec["rows"])
    for i, k in zip(idx, lab):
        out[i] = k
    return out


def overlap_share(a: list[int], b: list[int], w: np.ndarray) -> float:
    """Share of speech in each ``a`` label's most common ``b`` label (lines with b == -1 left out)."""
    tot: dict[int, dict[int, float]] = {}
    for x, y, s in zip(a, b, w):
        if y >= 0:
            tot.setdefault(x, {})[y] = tot.setdefault(x, {}).get(y, 0.0) + s
    whole = sum(sum(d.values()) for d in tot.values())
    return sum(max(d.values()) for d in tot.values()) / whole if whole else 1.0


def replay_row(recs: dict[str, dict], match: float, adopt: float, final_t: float) -> tuple[list[str], float, float]:
    cells, wrong, worst = [], 0.0, 1.0
    for name, rec in recs.items():
        w = (rec["rows"][:, 1] - rec["rows"][:, 0]) / 1000
        live, fin = live_labels(rec, match, adopt), final_labels(rec, final_t)
        pure, whole = overlap_share(live, fin, w), overlap_share(fin, [x if y >= 0 else -1 for x, y in zip(live, fin)], w)
        agree = min(pure, whole)
        n_fin = len({k for k in fin if k >= 0 and sum(w[i] for i, x in enumerate(fin) if x == k) >= 4.0})
        cell = f"{len(set(live))} live / {n_fin} final, {100 * agree:.0f}%"
        if rec["truth"]:
            pieces = [{"piece": i, "source": "system", "start_ms": int(r[0]), "end_ms": int(r[1]), "vec": i}
                      for i, r in enumerate(rec["rows"])]
            s, m, _tot = fl.truth_errors(rec["truth"], pieces, live, w)
            wrong += s + m
            cell += f", {s + m:.0f} s wrong"
        worst = min(worst, agree)
        cells.append(cell)
    return cells, wrong, worst


def cmd_replay(args) -> int:
    recs = load_lines(args.model)
    if not recs:
        print(f"no line cache for {args.model}; run embed first")
        return 1
    per_line = np.concatenate([r["embed_s"] for r in recs.values()])
    print(f"{args.model}: fingerprint per line median {1000 * np.median(per_line):.0f} ms, "
          f"p90 {1000 * np.percentile(per_line, 90):.0f} ms (1 thread)")
    print("| match | adopt | " + " | ".join(recs) + " | wrong s (truth) | worst agreement |")
    print("| --- | --- | " + " | ".join("---" for _ in recs) + " | --- | --- |")
    for match in [float(x) for x in args.match.split(",")]:
        for adopt in [float(x) for x in args.adopt.split(",")]:
            if adopt > match:
                continue
            cells, wrong, worst = replay_row(recs, match, adopt, args.final)
            print(f"| {match:.2f} | {adopt:.2f} | " + " | ".join(cells) + f" | {wrong:.0f} | {100 * worst:.0f}% |")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("embed", help="fingerprint every live system line with each model")
    p.add_argument("models", nargs="*")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("replay", help="replay live labelling and compare with truth and final speakers")
    p.add_argument("model")
    p.add_argument("--final", type=float, required=True, help="cluster_voices threshold for the reference speakers")
    p.add_argument("--match", default="0.35,0.40,0.45,0.50,0.55")
    p.add_argument("--adopt", default="0.25,0.30,0.35")
    p.set_defaults(fn=cmd_replay)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
