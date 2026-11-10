"""WP3 vocabulary spike: compare Whisper vocabulary prompts over recorded meeting copies.

Usage (from the repo root, PYTHONPATH=src):
  python tools/vocab_spike.py run FOLDER --variants v1,v2,e5   # one locked pass per variant
  python tools/vocab_spike.py diag FOLDER --base v1 --var v2 [--min-extra 4] [--quiet]
  python tools/vocab_spike.py table FOLDER v1 v2 e5 e5+T [--extras e5+T]
Reads FOLDER/audio/{system,mic}.flac (never writes there); writes lab/vocab/raw/*.json
(every segment with temperature, compression ratio, log-prob, no-speech prob, and each
word's timing and probability). "+T" in `table` applies a post-decode trim (TRIMS).
Each pass waits for lab/heavy.lock, creates it ("wp3"), and removes it afterwards.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

from huddle_helper import models
from huddle_helper.audio.buffer import read_audio
from huddle_helper.transcribe import _HALLUCINATIONS, _clean, drop_hallucinated, normalize_level
from huddle_helper.segments import Word
from huddle_helper.vad import UtteranceSegmenter

BASE = "KoVa, QDM, ETL, FMS, GCP, vendor, code freeze, prod, spreadsheet, zone code, Zentrix"
VARIANTS = {
    1: None,
    2: BASE,
    3: "Glossary: KoVa, QDM, ETL, FMS, GCP, vendor, code freeze, prod, spreadsheet, zone code, Zentrix.",
    4: BASE + ", Remote A, Remote B, Remote C, Remote D",
}
EXPECTED = ["KoVa", "QDM", "ETL", "FMS", "GCP", "vendor", "code freeze", "prod", "spreadsheet",
            "zone codes?", "Zentrix"]
MISHEARS = ["cover", "co-va", "co-ver", "quodium", "odium", "ETO", "FMX", "bender",
            "co-trees", "spray cheat", "zonkers", "zendrix"]
NAMES = ["Remote A", "Remote B", "Remote C", "Remote D"]
# vocabulary phrases for the vocabulary-only-line check (plural s tolerated)
VOCAB_PHRASES = ["KoVa", "QDM", "ETL", "FMS", "GCP", "vendors?", "code freeze", "prod",
                 "spreadsheets?", "zone codes?", "Zentrix"]


def rx(term: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + term.replace(" ", r"\s+") + r"(?![\w])", re.I)


def count(term: str, texts: list[str]) -> int:
    p = rx(term)
    return sum(len(p.findall(t)) for t in texts)


def vocab_only(text: str) -> bool:
    s = text
    for ph in sorted(VOCAB_PHRASES, key=len, reverse=True):
        s = rx(ph).sub(" ", s)
    s = re.sub(r"[\W_]+", "", s)
    return s == "" and re.search(r"\w", text) is not None


SENT = ("This call is about KoVa, QDM, ETL, FMS, GCP, vendors, the code freeze, prod, "
        "the spreadsheet, zone codes and Zentrix.")
# Decode variants (each needs a pass over the audio). Post-decode trims are applied offline.
DECODES: dict[str, dict] = {
    "v1": {},
    "v2": {"prompt": BASE, "mode": "hotwords"},
    "a": {"prompt": BASE, "mode": "initial_prompt"},
    "b": {"prompt": BASE, "mode": "hotwords", "temps": [0.0]},
    "d": {"prompt": SENT, "mode": "hotwords"},
    "d0": {"prompt": SENT, "mode": "hotwords", "temps": [0.0]},
    # stop_rem: stop reading segments once one ends within stop_rem seconds of the end of the
    # audio, so faster-whisper never decodes the next window over the leftover silence
    "e1": {"prompt": BASE, "mode": "hotwords", "stop_rem": 0.5},
    "e2": {"prompt": SENT, "mode": "hotwords", "stop_rem": 0.5},
    "e3": {"prompt": BASE, "mode": "hotwords", "stop_rem": 0.5, "temps": [0.0]},
    "e4": {"stop_rem": 0.5},
    "e7": {"stop_rem": 1.0},
    "e5": {"prompt": SENT, "mode": "hotwords", "stop_rem": 1.0},
    "e6": {"prompt": SENT, "mode": "hotwords", "stop_rem": 0.5, "temps": [0.0]},
    "e5b": {"prompt": SENT, "mode": "hotwords", "stop_rem": 1.0},  # e5 again: retries are random
    "e8": {"prompt": SENT, "mode": "hotwords", "stop_rem": 0.7},
}
LOCK = Path("lab/heavy.lock")
LOCK_POLL_S = 30
LOCK_WAIT_S = 3600
LOCK_STALE_S = 45 * 60


class HeavyLock:
    """lab/heavy.lock: wait until absent, create with 'wp3', delete only our own lock."""

    def __enter__(self):
        waited = 0
        while True:
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, b"wp3")
                os.close(fd)
                self.mine = True
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
                        print("stale lock (older than 45 min); removing", flush=True)
                        LOCK.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if waited >= LOCK_WAIT_S:
                    raise TimeoutError("heavy.lock still present after 60 min")
                time.sleep(LOCK_POLL_S)
                waited += LOCK_POLL_S

    def __exit__(self, *exc):
        try:
            if LOCK.read_text() == "wp3":
                LOCK.unlink()
        except FileNotFoundError:
            pass
        return False


def decode(model, audio: np.ndarray, cfg: dict) -> dict:
    """One Whisper call (as WhisperTranscriber.transcribe) logging everything it returns."""
    kw: dict = {}
    if cfg.get("prompt"):
        kw["hotwords" if cfg["mode"] == "hotwords" else "initial_prompt"] = cfg["prompt"]
    for k in ("no_speech_threshold", "log_prob_threshold", "compression_ratio_threshold"):
        if k in cfg:
            kw[k] = cfg[k]
    t0 = time.perf_counter()
    segments, _ = model.transcribe(
        normalize_level(audio), language="en", beam_size=1, vad_filter=False,
        word_timestamps=True, condition_on_previous_text=False, without_timestamps=False,
        temperature=cfg.get("temps", [0.0, 0.2, 0.4]), **kw,
    )
    segs = []
    dur = len(audio) / 16000
    for seg in segments:
        segs.append({
            "seek": seg.seek, "start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text,
            "temp": seg.temperature, "cr": round(seg.compression_ratio, 3),
            "alp": round(seg.avg_logprob, 4), "nsp": round(seg.no_speech_prob, 4),
            "words": [[w.word, round(w.start, 3), round(w.end, 3), round(w.probability, 4)]
                      for w in (seg.words or [])],
        })
        if cfg.get("stop_rem") and dur - seg.end < cfg["stop_rem"]:
            break
    return {"wall_s": time.perf_counter() - t0, "segs": segs}


def finish(segs: list[dict]) -> tuple[str, float]:
    """The transcriber's post-processing applied to logged segments."""
    texts, words = [], []
    lp = wt = 0.0
    for seg in segs:
        if seg["nsp"] > 0.6 and seg["alp"] < -1.0:
            continue
        t = seg["text"].strip()
        if not t:
            continue
        texts.append(t)
        dur = max(seg["end"] - seg["start"], 0.1)
        lp += seg["alp"] * dur
        wt += dur
        for w, st, en, _p in seg["words"]:
            if w.strip():
                words.append(Word(int(st * 1000), int(en * 1000), w))
    text = _clean(" ".join(texts))
    if words:
        words = drop_hallucinated(words)
        text = _clean("".join(w.text for w in words))
    if not text or text.lower() in _HALLUCINATIONS:
        return "", 0.0
    conf = math.exp(lp / wt) if wt else 0.0
    return text, max(0.0, min(1.0, conf))


def mmss(ms: int) -> str:
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"


RAW = "raw"


def raw_path(out: Path, tag: str, name: str) -> Path:
    return out / RAW / f"{tag}__{name}.json"


def cmd_run(a) -> int:
    folder = Path(a.folder)
    out = Path(a.out)
    (out / RAW).mkdir(parents=True, exist_ok=True)
    tag = folder.name
    from faster_whisper import WhisperModel

    model = WhisperModel(str(models.whisper_dir("base.en")), device="cpu", compute_type="int8",
                         local_files_only=True)
    utts = []
    for stream in ("system", "mic"):
        audio = read_audio(folder / "audio" / f"{stream}.flac")
        seg = UtteranceSegmenter(stream, utts.append)
        seg.feed(audio)
        seg.flush()
    print(f"== {tag}: {len(utts)} utterances", flush=True)
    for name in a.variants.split(","):
        cfg = DECODES[name]
        with HeavyLock():  # one pass over one recording per lock
            decode(model, utts[0].audio, cfg)  # warm-up
            rows = []
            t0 = time.perf_counter()
            for u in utts:
                r = decode(model, u.audio, cfg)
                rows.append({"stream": u.source, "start_ms": u.start_ms, "end_ms": u.end_ms,
                             "dur_s": round(u.duration_s, 3), **r})
            wall = time.perf_counter() - t0
        raw_path(out, tag, name).write_text(json.dumps({"cfg": cfg, "wall_s": wall, "utts": rows}),
                                            encoding="utf-8")
        w = sorted(r["wall_s"] for r in rows)
        print(f"{name}: total {wall:.1f}s median {w[len(w) // 2]:.2f} p90 {w[int(len(w) * .9)]:.2f} "
              f"max {w[-1]:.2f}", flush=True)
    return 0


def load(out: Path, tag: str, name: str) -> dict:
    d = json.loads(raw_path(out, tag, name).read_text(encoding="utf-8"))
    return {(u["stream"], u["start_ms"]): u for u in d["utts"]} | {"_wall": d["wall_s"]}


def nwords(text: str) -> int:
    return len(text.split())


# ---- post-decode trim -------------------------------------------------------------------

TRIM = {"late_alp": -0.8, "late_cr": 2.4, "late_nsp": 0.2, "late_mp": None, "min_word_s": None,
        "margin_s": None}


def trim_segs(segs: list[dict], dur_s: float, t: dict | None) -> list[dict]:
    """Drop junk at the end of an utterance. t=None leaves segments alone.

    1. a segment after the first is dropped when its avg_logprob < late_alp, its compression
       ratio > late_cr, its no_speech_prob > late_nsp, or its mean word probability < late_mp
       (each only if not None);
    2. from the end of the line, words that last under min_word_s, or start within margin_s
       of the end of the audio, are cut, until a word that does neither.
    """
    if not t or not segs:
        return segs

    def mp(g):
        ps = [w[3] for w in g["words"] if w[0].strip()]
        return sum(ps) / len(ps) if ps else 1.0

    out = []
    seen = False  # a live segment has been kept
    for g in segs:
        if g["nsp"] > 0.6 and g["alp"] < -1.0 or not g["text"].strip():
            out.append(g)  # finish() ignores these
            continue
        if seen and ((t.get("late_alp") is not None and g["alp"] < t["late_alp"])
                     or (t.get("late_cr") is not None and g["cr"] > t["late_cr"])
                     or (t.get("late_nsp") is not None and g["nsp"] > t["late_nsp"])
                     or (t.get("late_mp") is not None and mp(g) < t["late_mp"])):
            continue
        seen = True
        out.append(g)
    if t.get("min_word_s") is None and t.get("margin_s") is None:
        return out
    out = [dict(g, words=[list(w) for w in g["words"]]) for g in out]
    pos = [(gi, wi) for gi, g in enumerate(out) for wi, w in enumerate(g["words"]) if w[0].strip()]
    cut = set()
    for gi, wi in reversed(pos):
        w = out[gi]["words"][wi]
        if (t.get("margin_s") is not None and w[1] >= dur_s - t["margin_s"]) or (
                t.get("min_word_s") is not None and w[2] - w[1] < t["min_word_s"]):
            cut.add((gi, wi))
        else:
            break
    for gi, g in enumerate(out):
        g["words"] = [w for wi, w in enumerate(g["words"]) if (gi, wi) not in cut]
    return [g for g in out if g["words"] or not g["text"].strip()]


def texts_of(run: dict, t: dict | None = None) -> dict:
    res = {}
    for k, u in run.items():
        if k == "_wall":
            continue
        res[k] = finish(trim_segs(u["segs"], u["dur_s"], t))[0]
    return res


def flat_words(segs: list[dict]) -> list[dict]:
    out = []
    for si, g in enumerate(segs):
        if g["nsp"] > 0.6 and g["alp"] < -1.0:
            continue
        for w, st, en, p in g["words"]:
            if w.strip():
                out.append({"seg": si, "w": w.strip(), "s": st, "e": en, "p": p, "temp": g["temp"],
                            "cr": g["cr"], "alp": g["alp"]})
    return out


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0


def timing(run: dict) -> str:
    w = [u["wall_s"] for k, u in run.items() if k != "_wall"]
    return (f"total {run['_wall']:.1f}s median {pct(w, .5):.2f} p90 {pct(w, .9):.2f} "
            f"max {max(w):.2f} (sum of utterances {sum(w):.1f})")


def cmd_diag(a) -> int:
    out = Path(a.out)
    tag = Path(a.folder).name
    r1, r2 = load(out, tag, a.base), load(out, tag, a.var)
    print("base", a.base, timing(r1))
    print("var ", a.var, timing(r2))
    keys = [k for k in r1 if k != "_wall"]
    ratio = [(r2[k]["wall_s"] / max(r1[k]["wall_s"], 1e-3)) for k in keys]
    print(f"per-utterance slowdown ratio: median {pct(ratio, .5):.2f} p90 {pct(ratio, .9):.2f}")
    rows = []
    for k in keys:
        t1, _ = finish(r1[k]["segs"])
        t2, _ = finish(r2[k]["segs"])
        rows.append((k, t1, t2, nwords(t2) - nwords(t1)))
    ex4 = [r for r in rows if r[3] >= a.min_extra]
    ex2 = [r for r in rows if r[3] >= 2]
    print(f"lines with {a.min_extra}+ extra words: {len(ex4)}; 2+: {len(ex2)}")
    cnt = dict(n=0, later_seg=0, multi_seg_v2=0, multi_seg_v1=0, after_v1_end=0, after_audio_end=0,
               squeezed=0, lowprob=0, any_temp=0, tail_temp=0, hi_cr=0)
    print("\nstream mm:ss extra segs(v1->v2) temps crmax tail:[(seg,start,dur,p)...] v1end dur")
    for k, t1, t2, ex in ex4:
        u1, u2 = r1[k], r2[k]
        fw = flat_words(u2["segs"])
        f1 = flat_words(u1["segs"])
        tail = fw[-ex:]
        v1end = f1[-1]["e"] if f1 else 0.0
        dur = u2["dur_s"]
        cnt["n"] += 1
        cnt["multi_seg_v2"] += len(u2["segs"]) > 1
        cnt["multi_seg_v1"] += len(u1["segs"]) > 1
        cnt["later_seg"] += any(w["seg"] > 0 for w in tail)
        cnt["after_v1_end"] += all(w["s"] >= v1end - 0.05 for w in tail)
        cnt["after_audio_end"] += any(w["s"] >= dur - 0.3 for w in tail)
        cnt["squeezed"] += sum((w["e"] - w["s"]) < 0.04 for w in tail) >= max(1, ex // 2)
        mp = sum(w["p"] for w in tail) / len(tail)
        cnt["lowprob"] += mp < 0.5
        temps = sorted({g["temp"] for g in u2["segs"]})
        cnt["any_temp"] += any(t > 0 for t in temps)
        cnt["tail_temp"] += any(w["temp"] > 0 for w in tail)
        cnt["hi_cr"] += max(g["cr"] for g in u2["segs"]) > 2.0
        if a.quiet:
            continue
        print(k[0], mmss(k[1]), ex, f"{len(u1['segs'])}->{len(u2['segs'])}", temps,
              max(g["cr"] for g in u2["segs"]),
              [(w["seg"], w["s"], round(w["e"] - w["s"], 2), w["p"]) for w in tail],
              round(v1end, 2), dur)
    print("\ncounts over the 4+ lines:", cnt)
    slow = [(k, r) for k, r in zip(keys, ratio) if r > 2 and r2[k]["wall_s"] > 0.5]
    fb = [k for k, _ in slow if any(g["temp"] > 0 for g in r2[k]["segs"])]
    ms = [k for k, _ in slow if len(r2[k]["segs"]) > len(r1[k]["segs"])]
    print(f"slow (>2x v1 and >0.5s): {len(slow)}; with a fallback temp>0: {len(fb)}; "
          f"with more segments than v1: {len(ms)}")
    allfb = [k for k in keys if any(g["temp"] > 0 for g in r2[k]["segs"])]
    allfb1 = [k for k in keys if any(g["temp"] > 0 for g in r1[k]["segs"])]
    print(f"utterances with a fallback temp>0: {a.var}={len(allfb)} {a.base}={len(allfb1)}")
    sl_ms = [r2[k]["wall_s"] for k, _ in slow]
    print("slow utterance wall times:", sorted(round(x, 1) for x in sl_ms)[-10:])
    # correlation of time with fallback overall
    w_fb = [r2[k]["wall_s"] for k in allfb]
    w_no = [r2[k]["wall_s"] for k in keys if k not in set(allfb)]
    print(f"median wall with fallback {pct(w_fb, .5):.2f} (n={len(w_fb)}) without {pct(w_no, .5):.2f}")
    # per-utterance word counts correlation: seconds per decoded token
    return 0


TRIMS: dict[str, dict] = {
    "T": TRIM,
    "T0": {**TRIM, "late_cr": None},
    "Tw": {**TRIM, "min_word_s": 0.04},
    "Tm": {**TRIM, "late_mp": 0.15},
    "Tmw": {**TRIM, "late_mp": 0.15, "min_word_s": 0.04},
    "Ta": {**TRIM, "late_alp": -0.9, "late_nsp": None},  # alp only, looser
    "Tn": {**TRIM, "late_nsp": 0.15},
    "Tp": {**TRIM, "late_alp": None, "late_cr": None, "late_nsp": None},  # no-op, sanity check
}


def spec_texts(out: Path, tag: str, spec: str):
    name, _, tr = spec.partition("+")
    run = load(out, tag, name)
    return run, texts_of(run, TRIMS[tr] if tr else None)


def extra_tokens(t1: str, t2: str) -> list[str]:
    import difflib

    a, b = t1.split(), t2.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [x.lower().strip(".,?!") for x in a],
                                                      [x.lower().strip(".,?!") for x in b]).get_opcodes():
        if op == "insert" or (op == "replace" and (j2 - j1) > (i2 - i1)):
            out.extend(b[j1:j2])
    return out


def cmd_table(a) -> int:
    out = Path(a.out)
    tag = Path(a.folder).name
    run1, t1 = spec_texts(out, tag, "v1")
    print(f"{'variant':10} {'lines':>5} {'terms':>5} {'mish':>4} {'x4':>3} {'x2':>3} {'lost':>4} "
          f"{'chg':>4} {'new':>3} {'vonly':>5}  time")
    for spec in a.specs:
        run, t = spec_texts(out, tag, spec)
        texts = [x for x in t.values() if x]
        terms = sum(count(e, texts) for e in EXPECTED)
        mish = sum(1 for m in MISHEARS if count(m, texts))
        x4 = x2 = lost = chg = new = 0
        for k in t:
            if not t1[k] and t[k]:
                new += 1
            if t1[k] and not t[k]:
                lost += 1
            if t1[k] and t[k] and t[k] != t1[k]:
                chg += 1
            d = nwords(t[k]) - nwords(t1[k])
            x4 += d >= 4
            x2 += d >= 2
        vo = sum(1 for x in texts if vocab_only(x))
        print(f"{spec:10} {len(texts):5d} {terms:5d} {mish:4d} {x4:3d} {x2:3d} {lost:4d} {chg:4d} "
              f"{new:3d} {vo:5d}  {timing(run)}")
    if a.extras:
        run, t = spec_texts(out, tag, a.extras)
        print(f"-- lines with 2+ extra words, {a.extras}")
        for k in t:
            if nwords(t[k]) - nwords(t1[k]) >= 2:
                print(f"{k[0]} {mmss(k[1])} +{nwords(t[k]) - nwords(t1[k])}: {' '.join(extra_tokens(t1[k], t[k]))}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("folder")
    r.add_argument("--variants", default="v1,v2")
    r.add_argument("--out", default="lab/vocab")
    r.set_defaults(fn=cmd_run)
    d = sub.add_parser("diag")
    d.add_argument("folder")
    d.add_argument("--base", default="v1")
    d.add_argument("--var", default="v2")
    d.add_argument("--out", default="lab/vocab")
    d.add_argument("--min-extra", type=int, default=4)
    d.add_argument("--quiet", action="store_true")
    d.set_defaults(fn=cmd_diag)
    t = sub.add_parser("table")
    t.add_argument("folder")
    t.add_argument("specs", nargs="+", help="decode name, optionally +T for the trim")
    t.add_argument("--extras", default=None)
    t.add_argument("--out", default="lab/vocab")
    t.set_defaults(fn=cmd_table)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
