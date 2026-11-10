"""WP3 vocabulary spike: compare Whisper hotwords wordings over recorded meeting copies.

Usage: python tools/vocab_spike.py FOLDER [--variants 1,2,3,4]
Reads FOLDER/audio/{system,mic}.flac (never writes there); writes lines to lab/vocab/.
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


def transcribe(model, audio: np.ndarray, hotwords: str | None):
    """Copy of WhisperTranscriber.transcribe (beam 1) plus hotwords."""
    kw = {"hotwords": hotwords} if hotwords else {}
    segments, _ = model.transcribe(
        normalize_level(audio), language="en", beam_size=1, vad_filter=False,
        word_timestamps=True, condition_on_previous_text=False, without_timestamps=False,
        temperature=[0.0, 0.2, 0.4], **kw,
    )
    texts, words = [], []
    lp = wt = 0.0
    for seg in segments:
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
            continue
        t = seg.text.strip()
        if not t:
            continue
        texts.append(t)
        dur = max(seg.end - seg.start, 0.1)
        lp += seg.avg_logprob * dur
        wt += dur
        for w in seg.words or []:
            if w.word.strip():
                words.append(Word(int(w.start * 1000), int(w.end * 1000), w.word))
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--variants", default="1,2,3,4")
    ap.add_argument("--out", default="lab/vocab")
    a = ap.parse_args()
    folder = Path(a.folder)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = folder.name
    variants = [int(v) for v in a.variants.split(",")]

    from faster_whisper import WhisperModel

    model = WhisperModel(str(models.whisper_dir("base.en")), device="cpu", compute_type="int8",
                         local_files_only=True)
    print(f"== {tag}")
    for v in variants:
        if VARIANTS[v]:
            n = len(model.hf_tokenizer.encode(" " + VARIANTS[v], add_special_tokens=False).ids)
            print(f"variant {v} hotwords tokens: {n}")

    utts = []
    for stream in ("system", "mic"):
        audio = read_audio(folder / "audio" / f"{stream}.flac")
        seg = UtteranceSegmenter(stream, utts.append)
        seg.feed(audio)
        seg.flush()
    print(f"{len(utts)} utterances")
    # warm up so variant 1 does not pay one-off costs
    transcribe(model, utts[0].audio, None)

    runs = {}
    for v in variants:
        res = {}
        slowest = (0.0, "")
        t0 = time.perf_counter()
        for u in utts:
            t1 = time.perf_counter()
            text, conf = transcribe(model, u.audio, VARIANTS[v])
            d = time.perf_counter() - t1
            if d > slowest[0]:
                slowest = (d, f"{u.source} {mmss(u.start_ms)}")
            res[(u.source, u.start_ms)] = (u.end_ms, text, conf)
        wall = time.perf_counter() - t0
        runs[v] = (res, wall, slowest)
        with open(out / f"{tag}__v{v}.jsonl", "w", encoding="utf-8") as f:
            for (s, st), (en, text, conf) in res.items():
                if text:
                    f.write(json.dumps({"stream": s, "start_ms": st, "end_ms": en, "text": text,
                                        "confidence": round(conf, 4)}) + "\n")
        print(f"variant {v}: {wall:.1f}s, slowest {slowest[0]:.2f}s ({slowest[1]})")

    base = runs[variants[0]][0] if 1 in runs else None
    summary = {}
    for v in variants:
        res, wall, slowest = runs[v]
        texts = [t for (_, t, _) in res.values() if t]
        s = {"lines": len(texts), "wall_s": round(wall, 1),
             "slowest_s": round(slowest[0], 2), "slowest_at": slowest[1],
             "expected": {e.replace("s?", "(s)"): count(e, texts) for e in EXPECTED},
             "mishears": {m: count(m, texts) for m in MISHEARS},
             "vocab_only_lines": [t for t in texts if vocab_only(t)]}
        if base is not None and v != 1:
            s["new_vs_v1"] = [
                [k[0], mmss(k[1]), res[k][1]] for k in res
                if res[k][1] and not base[k][1]
            ]
            s["lost_vs_v1"] = sum(1 for k in res if base[k][1] and not res[k][1])
            s["changed_vs_v1"] = sum(1 for k in res if res[k][1] and base[k][1] and res[k][1] != base[k][1])
        if v == 4 and base is not None:
            ins = []
            for k, (_, t, _) in res.items():
                if t and any(rx(n).search(t) for n in NAMES) and \
                        not any(rx(n).search(base[k][1]) for n in NAMES):
                    ins.append([k[0], mmss(k[1]), base[k][1], t])
            s["name_insertions"] = ins
        summary[v] = s
    (out / f"{tag}__summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    print("\n-- terms")
    for v in variants:
        print(f"v{v} expected:", summary[v]["expected"])
        print(f"v{v} mishears:", {k: c for k, c in summary[v]["mishears"].items() if c})
    for v in variants:
        s = summary[v]
        print(f"v{v}: lines={s['lines']} new_vs_v1={len(s.get('new_vs_v1', []))} "
              f"lost={s.get('lost_vs_v1', '-')} changed={s.get('changed_vs_v1', '-')} "
              f"vocab_only={len(s['vocab_only_lines'])}")
        for x in s.get("new_vs_v1", []):
            print("   NEW", x)
        for x in s["vocab_only_lines"]:
            print("   VOCAB-ONLY", repr(x))
    for x in summary.get(4, {}).get("name_insertions", []):
        print("NAME", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
