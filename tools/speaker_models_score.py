"""Score each speaker model: pair separation on truth, and the threshold range that works on every recording."""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import final_lab as fl  # noqa: E402
from huddle_helper.diarize import cluster_voices, fold_small_clusters  # noqa: E402

RECS = {"test-2": "*test-2-2", "test-3": "*test-3", "test-4": "*work*", "test-5": "*test-meeting-4"}
GRID = np.round(np.arange(0.20, 0.901, 0.01), 2)


def load(model_dir):
    out = {}
    for name, pat in RECS.items():
        info, arr = fl.load_cache(next(model_dir.glob(pat + ".json")))
        pieces = info["pieces"]["system"]
        X, w = arr["system_vecs"].astype(np.float64), arr["system_weights"].astype(float)
        tp = ROOT / "tests" / "data" / "truth" / f"{name}.json"
        truth = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
        bp = ROOT / "lab" / "baseline" / name / "transcript.final.jsonl"
        base = fl.baseline_speakers(pieces, len(X), bp) if bp.exists() else None
        out[name] = dict(pieces=pieces, X=X, w=w, truth=truth, base=base, starts=fl.vec_starts(pieces, len(X)),
                         secs=info.get("timings_s", {}).get("embed_pieces"))
    return out


def pairs(rec, min_s):
    truth, pieces, X, w = rec["truth"], rec["pieces"], rec["X"], rec["w"]
    skip = set(truth.get("unchecked", []))
    groups = {g: [p["vec"] for p in fl.resolve_group(items, pieces)[0] if p["vec"] >= 0 and w[p["vec"]] >= min_s]
              for g, items in truth["groups"].items() if g not in skip}
    S = X @ X.T
    same = [S[i, j] for g in truth.get("together", []) for a, i in enumerate(groups[g]) for j in groups[g][a + 1:]]
    diff = [S[i, j] for a, b in truth.get("apart", []) for i in groups[a] for j in groups[b]]
    return np.array(same), np.array(diff)


def auc(s, d):
    return float((s[:, None] > d[None, :]).mean())


def pooled(recs, min_s):
    sd = [pairs(r, min_s) for r in recs.values() if r["truth"]]
    ts = np.linspace(-0.2, 1, 481)
    err = np.mean([[((s < t).mean() + (d >= t).mean()) / 2 for t in ts] for s, d in sd], axis=0)
    return float(err.min()), float(np.mean([auc(s, d) for s, d in sd])), float(min(auc(s, d) for s, d in sd))


def test2_ok(rec, labels):
    base, w = rec["base"], rec["w"]
    main = sorted({b for b in base if b}, key=lambda b: -sum(x for x, y in zip(w, base) if y == b))[:2]
    keep = [i for i, b in enumerate(base) if b in main and w[i] >= 3.0]
    pr = {(base[i], labels[i]) for i in keep}
    return len({b for b, _ in pr}) == len(pr) == len({lab for _, lab in pr})


def window(recs, anchor_s):
    """Per threshold: wrong seconds over the truth recordings, and whether Test #2 keeps its two main speakers."""
    wrong, t2 = np.zeros(len(GRID)), np.zeros(len(GRID), bool)
    per = {n: np.zeros(len(GRID)) for n in recs if recs[n]["truth"]}
    for k, t in enumerate(GRID):
        for name, r in recs.items():
            v, w = list(r["X"]), list(r["w"])
            raw = fl.anchors_first(v, w, r["starts"], t, anchor_s, -9.0) if anchor_s else cluster_voices(v, w, t)
            lab = fold_small_clusters(raw, v, w, min_similarity=t)
            if r["truth"]:
                s, m, _ = fl.truth_errors(r["truth"], r["pieces"], lab, w)
                per[name][k] = s + m
                wrong[k] += s + m
            else:
                t2[k] = test2_ok(r, lab)
    return wrong, t2, per


def span(mask):
    idx = np.flatnonzero(mask)
    if not len(idx):
        return "none", 0
    # longest contiguous run
    runs, start = [], idx[0]
    for a, b in zip(idx, idx[1:]):
        if b != a + 1:
            runs.append((start, a))
            start = b
    runs.append((start, idx[-1]))
    a, b = max(runs, key=lambda r: r[1] - r[0])
    return f"{GRID[a]:.2f}-{GRID[b]:.2f}", int(b - a + 1)


def main():
    rows = []
    for d in sorted((ROOT / "lab" / "cache_models").iterdir()):
        recs = load(d)
        e_all, auc_all, aucmin_all = pooled(recs, 0.0)
        e_long, auc_long, aucmin_long = pooled(recs, 3.0)
        out = [d.name.replace("3dspeaker_speech_", "3ds_").replace("_16k", "").replace("wespeaker_en_voxceleb_", "wespeaker_"),
               f"{100*e_all:.1f}", f"{aucmin_all:.3f}", f"{100*e_long:.1f}", f"{aucmin_long:.3f}"]
        for anchor_s in (0.0, 4.0):
            wrong, t2, per = window(recs, anchor_s)
            s3, n3 = span(wrong <= 2.0)
            s4, n4 = span((wrong <= 2.0) & t2)
            out += [f"{wrong.min():.1f}", f"{s3} ({n3})", f"{s4} ({n4})"]
            if "-v" in sys.argv:
                print(d.name, "anchors", anchor_s, {n: span(p <= 2.0) for n, p in per.items()}, "t2", span(t2))
        out.append(f"{recs['test-4']['secs']}")
        rows.append(out)
    head = ["model", "pair err % all", "worst AUC all", "pair err % >=3s", "worst AUC >=3s",
            "plain: min wrong s", "plain: range T3-5", "plain: range +T2",
            "anchors4: min wrong s", "anchors4: range T3-5", "anchors4: range +T2", "T4 embed s"]
    print("| " + " | ".join(head) + " |")
    print("|" + " --- |" * len(head))
    for r in rows:
        print("| " + " | ".join(r) + " |")


if __name__ == "__main__":
    main()
