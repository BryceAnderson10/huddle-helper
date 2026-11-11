"""Public baseline: AMI Meeting Corpus meetings as two-stream test meetings (lab tool).

    python tools/ami_lab.py fetch ES2004a IS1009a TS3003a EN2002a
    python tools/ami_lab.py convert ES2004a [--mic A]
    python tools/ami_lab.py score lab/ami/runs/<meeting folder> [--show 10]

Run from the repo root. Everything is written under ``lab/ami`` (never in git).

``fetch`` downloads each meeting's individual headset recordings and, once, the manual
annotations (transcripts) from the University of Edinburgh. AMI is CC BY 4.0: say so wherever a
result from it is published.

``convert`` writes ``lab/ami/<meeting>/``: ``mic.wav`` is one speaker's headset ("You"),
``system.wav`` is the other headsets added together (the far side of a call), plus
``ref.mic.txt``, ``ref.system.txt`` and ``ref.all.txt`` for ``tools/wer.py`` and ``truth.json`` (who
spoke when). The four people sat in one room, so every headset also hears the other three. A call
is not like that, and with the raw headsets the far side carries the mic speaker's words too (the
app then drops the mic's copy as echo). So each headset is silenced while its own speaker is not
talking (``--no-gate`` keeps the raw headsets).
Word fragments the transcribers marked as cut off are left out of the references. Then::

    huddle-helper simulate --mic lab/ami/<m>/mic.wav --system lab/ami/<m>/system.wav \
        --title ami-<m> --out lab/ami/runs --keep-audio

``score`` prints, for the live and the final transcript of a run: word error rate per stream and for
both together ("all": every line in time order against every speaker's words in time order),
as it stands and with filler words ("um", "uh", "mm-hmm" ...) taken out of both sides, and for
the system stream how the app's speakers line up with the real ones. A line's real speaker is the
one who talks most while it lasts. Purity: share of line time whose app speaker is mostly that one
real speaker. Coverage: share of line time where a real speaker is under their main app speaker.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wer  # noqa: E402

ROOT = Path("lab/ami")
AUDIO_URL = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/{m}.Headset-{k}.wav"
ANNOTATIONS_URL = "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"
RATE = 16000
TURN_GAP_MS = 500  # words of one speaker closer than this are one turn
GATE_PAD_MS = 250  # a headset is kept this long before and after its own speaker's turns
FILLERS = frozenset("um uh uhm erm er eh ah hmm hm mm mhm mmhmm uhhuh huh".split())
_MEETING = re.compile(r"^[A-Z]{2}\d{4}[a-z]$")


def _download(url: str, dest: Path) -> bool:
    """Fetch url to dest unless it is there already; False when the server does not have it."""
    if dest.exists() and dest.stat().st_size:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code == 404:
            return False
        raise
    part.replace(dest)
    return True


def annotations() -> zipfile.ZipFile:
    path = ROOT / "ann" / "manual.zip"
    _download(ANNOTATIONS_URL, path)
    return zipfile.ZipFile(path)


def cmd_fetch(args) -> int:
    annotations().close()
    for m in args.meetings:
        got = [k for k in range(5) if _download(AUDIO_URL.format(m=m, k=k), ROOT / "raw" / m / f"{m}.Headset-{k}.wav")]
        print(f"{m}: headsets {got}")
    return 0


# -- annotations ----------------------------------------------------------------------------------
def speakers(z: zipfile.ZipFile, meeting: str) -> dict[str, dict]:
    """{letter: {"channel": headset number, "id": the corpus's speaker id}}."""
    root = ET.fromstring(z.read("corpusResources/meetings.xml"))
    for node in root.iter("meeting"):
        if node.get("observation") == meeting:
            return {s.get("nxt_agent"): {"channel": int(s.get("channel")), "id": s.get("global_name")}
                    for s in node.iter("speaker")}
    raise SystemExit(f"{meeting} is not in the annotations")


def words(z: zipfile.ZipFile, meeting: str, letter: str) -> list[tuple[int, int, str]]:
    """(start ms, end ms, word) in time order; punctuation, noises and cut-off words left out."""
    out = []
    for w in ET.fromstring(z.read(f"words/{meeting}.{letter}.words.xml")).iter("w"):
        if w.get("punc") or w.get("trunc") or not (w.text or "").strip() or w.get("starttime") is None:
            continue
        out.append((round(float(w.get("starttime")) * 1000), round(float(w.get("endtime")) * 1000), w.text.strip()))
    return sorted(out)


def turns(ws: list[tuple[int, int, str]]) -> list[list[int]]:
    out: list[list[int]] = []
    for start, end, _ in ws:
        if out and start - out[-1][1] < TURN_GAP_MS:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def cmd_convert(args) -> int:
    import soundfile as sf

    m = args.meeting
    z = annotations()
    who = speakers(z, m)
    if args.mic not in who:
        raise SystemExit(f"{m} has speakers {sorted(who)}, not {args.mic}")
    out = ROOT / m
    out.mkdir(parents=True, exist_ok=True)
    audio = {}
    for letter, s in who.items():
        data, rate = sf.read(str(ROOT / "raw" / m / f"{m}.Headset-{s['channel']}.wav"), dtype="float32", always_2d=True)
        if rate != RATE:
            raise SystemExit(f"{m} headset {s['channel']} is {rate} Hz, expected {RATE}")
        audio[letter] = data.mean(axis=1)
    by = {letter: words(z, m, letter) for letter in who}
    if args.gate:
        audio = {letter: gate(a, turns(by[letter])) for letter, a in audio.items()}
    n = max(len(a) for a in audio.values())
    system = np.zeros(n, np.float32)
    for letter, a in audio.items():
        if letter != args.mic:
            system[:len(a)] += a
    peak = float(np.abs(system).max())
    if peak > 0.99:
        system *= 0.99 / peak
    sf.write(str(out / "mic.wav"), audio[args.mic], RATE, subtype="PCM_16")
    sf.write(str(out / "system.wav"), system, RATE, subtype="PCM_16")

    far = sorted((w + (letter,) for letter in who if letter != args.mic for w in by[letter]))
    header = f"# AMI Meeting Corpus {m} (CC BY 4.0), manual transcript, "
    (out / "ref.mic.txt").write_text(header + f"speaker {args.mic}\n" + _lines(by[args.mic]), encoding="utf-8")
    (out / "ref.system.txt").write_text(header + "all speakers but " + args.mic + "\n" + _lines(far), encoding="utf-8")
    everyone = sorted(w + (letter,) for letter in who for w in by[letter])
    (out / "ref.all.txt").write_text(header + "all speakers\n" + _lines(everyone), encoding="utf-8")
    truth = {"meeting": m, "mic": args.mic, "gated": args.gate, "duration_s": round(n / RATE, 1),
             "speakers": {k: v["id"] for k, v in who.items()},
             "turns": sorted([s, e, letter] for letter in who for s, e in turns(by[letter]))}
    (out / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    print(f"{m}: {n / RATE / 60:.1f} min, mic = {args.mic} ({len(by[args.mic])} words), "
          f"system = {len(far)} words from {len(who) - 1} speakers -> {out}")
    return 0


def gate(audio: np.ndarray, spans: list[list[int]], pad_ms: int = GATE_PAD_MS, fade_ms: int = 20) -> np.ndarray:
    """The audio, silent outside the spans (each widened by pad_ms, with a short fade at both ends)."""
    keep = np.zeros(len(audio), np.float32)
    for start, end in spans:
        keep[max(0, (start - pad_ms) * RATE // 1000):(end + pad_ms) * RATE // 1000] = 1.0
    n = fade_ms * RATE // 1000
    keep = np.convolve(keep, np.ones(n, np.float32) / n, mode="same")
    return (audio * keep).astype(np.float32)


def _lines(ws: list[tuple], per_line: int = 16) -> str:
    return "\n".join(" ".join(w[2] for w in ws[i:i + per_line]) for i in range(0, len(ws), per_line)) + "\n"


# -- score ----------------------------------------------------------------------------------------
def word_errors(ref_text: str, hyp_text: str, fillers: bool = True) -> wer.Score:
    ref, hyp = wer.normalize(ref_text), wer.normalize(hyp_text)
    if not fillers:
        ref, hyp = ([w for w in side if w not in FILLERS] for side in (ref, hyp))
    ops = wer.align(ref, hyp)
    kinds = defaultdict(int)
    for op, _r, _h in ops:
        kinds[op] += 1
    return wer.Score(len(ops) - kinds["ins"], len(ops) - kinds["del"], kinds["sub"], kinds["del"], kinds["ins"]), ops


def overlap_ms(start: int, end: int, spans: list[tuple[int, int]]) -> int:
    return sum(max(0, min(end, e) - max(start, s)) for s, e in spans)


def speaker_table(lines: list[dict], truth: dict) -> tuple[dict, float, float, float]:
    """({app speaker: {real speaker or "-": seconds}}, purity, coverage, share of line time with no speech)."""
    spans = defaultdict(list)
    for s, e, letter in truth["turns"]:
        if not (truth.get("gated") and letter == truth.get("mic")):  # gated: the mic speaker is not in the stream
            spans[letter].append((s, e))
    table: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for line in lines:
        got = {letter: overlap_ms(line["start_ms"], line["end_ms"], sp) for letter, sp in spans.items()}
        real = max(got, key=got.get) if any(got.values()) else "-"
        table[line.get("speaker") or "?"][real] += (line["end_ms"] - line["start_ms"]) / 1000
    total = sum(sum(row.values()) for row in table.values())
    spoken = sum(v for row in table.values() for k, v in row.items() if k != "-")
    by_real: dict[str, float] = defaultdict(float)
    for row in table.values():
        for k, v in row.items():
            if k != "-":
                by_real[k] = max(by_real[k], v)
    purity = sum(max((v for k, v in row.items() if k != "-"), default=0.0) for row in table.values()) / (spoken or 1)
    return table, purity, sum(by_real.values()) / (spoken or 1), (total - spoken) / (total or 1)


def meeting_in(name: str) -> str | None:
    """The AMI meeting id in a run folder's name ("2026-11-11_0312_ami-is1009a" -> "IS1009a")."""
    for part in re.split(r"[_\-]", name):
        m = part[:2].upper() + part[2:].lower()
        if _MEETING.match(m):
            return m
    return None


def cmd_score(args) -> int:
    folder = Path(args.folder)
    m = args.meeting or meeting_in(folder.name)
    if not m or not (ROOT / m / "truth.json").exists():
        raise SystemExit(f"which AMI meeting is {folder.name}? pass --meeting (and run convert first)")
    truth = json.loads((ROOT / m / "truth.json").read_text(encoding="utf-8"))
    print(f"{folder.name}: AMI {m}, {truth['duration_s'] / 60:.1f} min, mic = speaker {truth['mic']}")
    for name in ("transcript.jsonl", "transcript.final.jsonl"):
        path = folder / name
        if not path.exists():
            continue
        lines = wer.load_lines(path)
        print(f"\n{name}")
        for src in ("system", "mic", "all"):
            ref = wer.load_ref(ROOT / m / f"ref.{src}.txt")[0]
            hyp = wer.hyp_text(lines, None if src == "all" else src, None, None)
            for label, fillers in (("as is", True), ("no fillers", False)):
                s, ops = word_errors(ref, hyp, fillers)
                print(f"  {src:6s} {label:10s} WER {s.wer:6.1%}  ({s.substitutions} substituted, {s.deletions} missing, "
                      f"{s.insertions} added; {s.ref_words} reference words)")
            if args.show:
                errors = defaultdict(int)
                for op, r, h in ops:
                    if op != "ok":
                        errors[(op, r, h)] += 1
                for (op, r, h), n in sorted(errors.items(), key=lambda kv: -kv[1])[:args.show]:
                    print(f"       {n:3d}  {op}  {r or '-'} -> {h or '-'}")
        system = [r for r in lines if r.get("source") == "system"]
        table, purity, coverage, silent = speaker_table(system, truth)
        real = sorted(k for k in truth["speakers"] if k != truth["mic"])
        cols = real + ([] if truth.get("gated") else [truth["mic"]]) + ["-"]
        print(f"  system speakers: app found {len(table)}, really {len(real)}; purity {purity:.1%}, "
              f"coverage {coverage:.1%}, {silent:.1%} of line time with nobody talking")
        print("    seconds by real speaker    " + "".join(f"{c + ('*' if c == truth['mic'] else ''):>8s}" for c in cols))
        for sp, row in sorted(table.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"    {sp[:26]:26s} " + "".join(f"{row.get(c, 0.0):8.0f}" for c in cols))
        print("    (- nobody)" if truth.get("gated") else
              "    (* the mic speaker, heard on the others' headsets; - nobody)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("meetings", nargs="+")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("convert")
    p.add_argument("meeting")
    p.add_argument("--mic", default="A", help="the speaker whose headset becomes mic.wav (default A)")
    p.add_argument("--gate", action=argparse.BooleanOptionalAction, default=True,
                   help="silence each headset while its own speaker is not talking (default on)")
    p.set_defaults(func=cmd_convert)
    p = sub.add_parser("score")
    p.add_argument("folder")
    p.add_argument("--meeting", help="AMI meeting id, when the folder name does not hold it")
    p.add_argument("--show", type=int, default=0, help="print the N most frequent errors per stream")
    p.set_defaults(func=cmd_score)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
