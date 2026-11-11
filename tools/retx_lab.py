"""WP14 lab: transcribe every live line's audio again with another model, and compare the runs.

    python tools/retx_lab.py run lab/src/<meeting> --tags b1,s5,s5v,p [--threads 4] [--no-lock]
    python tools/retx_lab.py report [--meeting <part of the name>] [--examples 10]
    python tools/retx_lab.py hotwords          # how the vocabulary is cut into Parakeet's tokens

Run from the repo root. ``run`` reads ``<meeting>/transcript.jsonl`` and ``audio/{system,mic}.flac``
(it never writes there) and writes ``lab/retx/<meeting>__<tag>.json``: one row per live line with
the new text, a confidence, the seconds it took and each word's time in meeting milliseconds.
A tag is a variant name (``VARIANTS``), optionally followed by ``.label`` to keep several runs of
one variant apart (``s5.t`` is a second small.en beam 5 run).

Each line's span is its own audio from 200 ms before its start to 300 ms after its end. The
``pl`` variants join neighbouring lines of one source (gap under 1.5 s, at most 20 s per span),
transcribe the joined span once and hand each word back to the line it falls in.

Two back ends sit behind ``Backend``: the app's ``WhisperTranscriber`` and sherpa-onnx's offline
recognizer with NVIDIA Parakeet TDT 0.6B v2 (int8) from ``lab/models/asr``. The Whisper vocabulary
is the app's prompt sentence (``vocab.build_prompt``); Parakeet's is a sherpa-onnx hotwords file,
which needs ``modified_beam_search`` and a ``bpe.vocab`` that the model archive does not ship, so
one is written from ``tokens.txt`` (see ``write_bpe_vocab``).

``report`` prints, per meeting: time, memory, empty and odd lines, invented line ends, expected
terms and known mishears (only for a meeting that has a vocabulary run), the word-level
difference between every two runs, how far word start times are from the reference run's, and
the word error rate when ``lab/ref/<meeting>.txt`` exists (``tools/wer.py``). Its examples print
transcript text: keep that output under ``lab\\``.

Each ``run`` pass waits for ``lab/heavy.lock`` (``vocab_spike.HeavyLock``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vocab_spike as vs  # noqa: E402  (term lists, count(), HeavyLock)
import wer  # noqa: E402

from huddle_helper import vocab  # noqa: E402
from huddle_helper.audio import SAMPLE_RATE  # noqa: E402
from huddle_helper.audio.buffer import read_audio  # noqa: E402
from huddle_helper.transcribe import MIN_MEAN_WORD_MS, normalize_level  # noqa: E402

PRE_MS, POST_MS = 200, 300
JOIN_GAP_MS = 1500
JOIN_MAX_MS = 20_000
OUT = Path("lab/retx")
ASR_DIR = Path("lab/models/asr")
PARAKEET = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
PARAKEET_ARCHIVE_SHA256 = "6fc4426b54aa549a0019b8a75b8dea59164e71790a37ac56a045ad57f915f714"
PARAKEET_FILES_SHA256 = {  # as extracted; checked before the model is loaded
    "encoder.int8.onnx": "92a10bd9c23936ea0566c29f963bc57c2a5df4261b6a8921c39abea66557ca8b",
    "decoder.int8.onnx": "4d097bae3bac5c96018d5293ba39f1b657296102b142affda40378f38b020f41",
    "joiner.int8.onnx": "77a29f3e5ac7d37074e0af465391b80f314015d5b97f0f90c6c7767f1d56f428",
    "tokens.txt": "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d",
}
DEFAULT_TERMS = vs.BASE  # the vocabulary of the meeting the spike was run on
# sherpa-onnx's default. In 2.0.1's NeMo beam search this one number is used twice: added to the
# score of every token that could continue (or start) a hotword before the candidates are picked,
# which is never taken back, and again as the context graph's per-token bonus, which is taken back
# when the hotword does not complete. A per-term score in the hotwords file ("term :2.0") replaces
# only the second use. See offline-transducer-modified-beam-search-nemo-decoder.cc.
HOTWORDS_SCORE = 1.5
FRAME_S = 0.08  # Parakeet's encoder step; the shortest word it can time


@dataclass(frozen=True)
class Variant:
    backend: str  # "whisper" or "parakeet"
    model: str
    beam: int = 1  # Whisper beam size; Parakeet: 1 = greedy search, more = modified beam search paths
    vocab: bool = False
    joined: bool = False


VARIANTS = {
    "b1": Variant("whisper", "base.en", 1),  # the control: the live model on the same spans
    "b5": Variant("whisper", "base.en", 5),
    "s1": Variant("whisper", "small.en", 1),
    "s5": Variant("whisper", "small.en", 5),
    "s5v": Variant("whisper", "small.en", 5, vocab=True),
    "p": Variant("parakeet", PARAKEET),
    "pl": Variant("parakeet", PARAKEET, joined=True),
    "pb": Variant("parakeet", PARAKEET, beam=4),  # beam search without hotwords: the control for pv
    "pv": Variant("parakeet", PARAKEET, beam=4, vocab=True),
    "plv": Variant("parakeet", PARAKEET, beam=4, vocab=True, joined=True),
}


def variant_of(tag: str) -> Variant:
    return VARIANTS[tag.split(".")[0]]


# -- back ends ----------------------------------------------------------------------------------
@dataclass
class Out:
    text: str
    conf: float | None = None  # 0..1, or None when the back end has none
    words: list[tuple[int, int, str]] = field(default_factory=list)  # (start ms, end ms, text) from the span start
    tokens: list[str] | None = None  # the model's own word pieces, when the back end gives them


class Backend(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray) -> Out: ...


class WhisperBackend:
    """The app's transcriber, unchanged; ``terms`` become its prompt sentence."""

    def __init__(self, model: str, beam: int, threads: int, terms: list[str] | None):
        from huddle_helper.transcribe import WhisperTranscriber

        self.name = model
        self.prompt = vocab.build_prompt(terms) if terms else None
        self._tr = WhisperTranscriber(model, beam_size=beam, cpu_threads=threads)

    def transcribe(self, audio: np.ndarray) -> Out:
        r = self._tr.transcribe(audio, self.prompt)
        return Out(r.text, r.confidence, [(w.start_ms, w.end_ms, w.text.strip()) for w in r.words])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_ranks(tokens: Path) -> dict[str, int]:
    """Token -> id from ``tokens.txt``. The table is in SentencePiece BPE order: merged pieces by
    rank, then single characters, so a lower id is a merge learnt earlier."""
    ranks = {}
    for line in Path(tokens).read_text(encoding="utf-8").splitlines():
        token, _, idx = line.rpartition(" ")
        if token and token not in ("<blk>", "<unk>"):
            ranks[token] = int(idx)
    return ranks


def bpe_merge(word: str, ranks: dict[str, int]) -> list[str] | None:
    """One word cut the way the model's own tokenizer cuts it (SentencePiece BPE): start from
    single characters and keep merging the neighbouring pair whose merged piece has the best rank.
    None when a character is not in the table. ``hotwords --check`` compares this with the pieces
    the model itself wrote."""
    pieces = list("▁" + word)
    if any(c not in ranks for c in pieces):
        return None
    while True:
        best = min(((ranks[a + b], i) for i, (a, b) in enumerate(zip(pieces, pieces[1:])) if a + b in ranks),
                   default=None)
        if best is None:
            return pieces
        pieces[best[1]:best[1] + 2] = [pieces[best[1]] + pieces[best[1] + 1]]


def dp_cut(word: str, scores: dict[str, float]) -> list[str] | None:
    """One word cut the way sherpa-onnx cuts a hotword with a "bpe.vocab" (simple-sentencepiece):
    the split with the highest sum of scores, the longer piece on a tie."""
    text = "▁" + word
    best: list[tuple[float, int]] = [(-math.inf, -1)] * len(text) + [(0.0, -1)]
    for i in range(len(text) - 1, -1, -1):
        for j in range(i + 1, len(text) + 1):
            piece = text[i:j]
            if piece in scores and best[j][0] > -math.inf and scores[piece] + best[j][0] >= best[i][0]:
                best[i] = (scores[piece] + best[j][0], j)
    out, i = [], 0
    while i < len(text):
        j = best[i][1]
        if j < 0:
            return None
        out.append(text[i:j])
        i = j
    return out


def naive_scores(ranks: dict[str, int]) -> dict[str, float]:
    """What SentencePiece itself would export for a BPE model: minus the rank."""
    return {tok: -float(max(idx - 1, 0)) for tok, idx in ranks.items()}


def hotword_scores(ranks: dict[str, int], hotwords: list[str]) -> tuple[dict[str, float], list[str]]:
    """Scores for a "bpe.vocab" under which sherpa-onnx cuts every hotword exactly as the model's
    tokenizer would. The archive ships only ``tokens.txt``, and with plain minus-rank scores the
    best-sum cut is often not the tokenizer's (pieces the model never writes for that word, so the
    hotword can never match). So the pieces of the wanted cuts score -1, every other piece -100,
    and each hotword is checked. Returns (scores, hotwords that cannot be encoded this way)."""
    cuts = {w: bpe_merge(w, ranks) for line in hotwords for w in line.split()}
    wanted = {piece for cut in cuts.values() if cut for piece in cut}
    scores = {tok: (-1.0 if tok in wanted else -100.0) for tok in ranks}
    bad = [line for line in hotwords
           if any(cuts[w] is None or dp_cut(w, scores) != cuts[w] for w in line.split())]
    return scores, bad


def write_bpe_vocab(scores: dict[str, float], dest: Path) -> None:
    dest.write_text("<unk>\t0\n" + "".join(f"{tok}\t{score:g}\n" for tok, score in scores.items()),
                    encoding="utf-8")


def hotword_lines(terms: list[str]) -> list[str]:
    """Each term as written, plus a capitalised form of lower-case terms (the model writes
    sentence starts with a capital, and a hotword only matches its exact tokens)."""
    out: list[str] = []
    for term in terms:
        for form in (term, term[:1].upper() + term[1:]):
            if form not in out:
                out.append(form)
    return out


class ParakeetBackend:
    """sherpa-onnx offline recognizer with a NeMo TDT transducer. ``beam`` 1 is greedy search;
    hotwords need modified beam search."""

    def __init__(self, model: str, beam: int, threads: int, terms: list[str] | None,
                 hotwords_score: float = HOTWORDS_SCORE, asr_dir: Path = ASR_DIR, work: Path = OUT,
                 bpe_scores: str = "fitted", line_score: float = 0.0):
        import sherpa_onnx

        folder = Path(asr_dir) / model
        for name, want in PARAKEET_FILES_SHA256.items():
            got = sha256(folder / name)
            if got != want:
                raise RuntimeError(f"{folder / name}: SHA-256 {got}, expected {want}")
        self.name = model
        self.hotwords = hotword_lines(terms) if terms else []
        extra: dict = {}
        if beam > 1:
            extra.update(decoding_method="modified_beam_search", max_active_paths=beam)
        if self.hotwords:
            if beam <= 1:
                raise ValueError("hotwords need modified beam search (beam > 1)")
            work = Path(work) / "hotwords"
            work.mkdir(parents=True, exist_ok=True)
            ranks = load_ranks(folder / "tokens.txt")
            scores, bad = (hotword_scores(ranks, self.hotwords) if bpe_scores == "fitted"
                           else (naive_scores(ranks), []))
            if bad:
                print("hotwords left out (cannot be cut into the model's tokens):", bad, flush=True)
                self.hotwords = [h for h in self.hotwords if h not in bad]
            # "term :2.0" gives the term its own score in sherpa-onnx's context graph (HOTWORDS_SCORE's note)
            mark = f" :{line_score:g}" if line_score else ""
            key = hashlib.sha256("\n".join([bpe_scores, mark, *self.hotwords]).encode()).hexdigest()[:8]
            write_bpe_vocab(scores, work / f"bpe_{key}.vocab")
            (work / f"hotwords_{key}.txt").write_text("".join(f"{h}{mark}\n" for h in self.hotwords),
                                                      encoding="utf-8")
            extra.update(hotwords_file=str(work / f"hotwords_{key}.txt"), hotwords_score=hotwords_score,
                         modeling_unit="bpe", bpe_vocab=str(work / f"bpe_{key}.vocab"))
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(folder / "encoder.int8.onnx"), decoder=str(folder / "decoder.int8.onnx"),
            joiner=str(folder / "joiner.int8.onnx"), tokens=str(folder / "tokens.txt"),
            model_type="nemo_transducer", num_threads=max(1, threads), **extra)

    def transcribe(self, audio: np.ndarray) -> Out:
        # The model card asks for gain normalisation of quiet input; the app's is used.
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, normalize_level(audio))
        self._rec.decode_stream(stream)
        r = stream.result
        text = " ".join(r.text.split())
        if not text:
            return Out("", 0.0, [])
        probs = list(getattr(r, "ys_log_probs", None) or [])
        conf = max(0.0, min(1.0, math.exp(sum(probs) / len(probs)))) if probs else None
        return Out(text, conf, token_words(list(r.tokens), list(r.timestamps), list(r.durations or [])),
                   list(r.tokens))


def token_words(tokens: list[str], starts: list[float], durations: list[float]) -> list[tuple[int, int, str]]:
    """Words from sherpa-onnx tokens: a token with a leading space starts a word; the rest (word
    pieces, punctuation) join the word before. A word runs from its first token's time to its last
    token's time plus that token's length (at least one encoder step), but not past the next word."""
    groups: list[list[int]] = []
    for i, tok in enumerate(tokens):
        if tok.startswith((" ", "▁")) or not groups:
            groups.append([])
        groups[-1].append(i)
    words = []
    for g, idx in enumerate(groups):
        text = "".join(tokens[i] for i in idx).replace("▁", " ").strip()
        if not text:
            continue
        start = starts[idx[0]]
        last = idx[-1]
        end = starts[last] + max(durations[last] if last < len(durations) else 0.0, FRAME_S)
        if g + 1 < len(groups):
            end = max(start, min(end, starts[groups[g + 1][0]]))
        words.append((int(round(start * 1000)), int(round(end * 1000)), text))
    return words


def make_backend(v: Variant, threads: int, terms: list[str] | None, hotwords_score: float,
                 asr_dir: Path, out: Path, bpe_scores: str = "fitted", line_score: float = 0.0) -> Backend:
    terms = terms if v.vocab else None
    if v.backend == "whisper":
        return WhisperBackend(v.model, v.beam, threads, terms)
    return ParakeetBackend(v.model, v.beam, threads, terms, hotwords_score, asr_dir, out, bpe_scores, line_score)


# -- spans --------------------------------------------------------------------------------------
def load_segments(folder: Path) -> list[dict]:
    with open(folder / "transcript.jsonl", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return [r for r in rows if r.get("type") == "segment"]


def join_lines(segs: list[dict], joined: bool) -> list[list[int]]:
    """Indexes of the lines transcribed together, in file order of each group's first line.
    Not joined: every line alone. Joined: neighbours of one source while the gap is under
    JOIN_GAP_MS and the whole span (with its padding) stays within JOIN_MAX_MS."""
    if not joined:
        return [[i] for i in range(len(segs))]
    groups: list[list[int]] = []
    open_group: dict[str, list[int]] = {}
    for i, s in enumerate(segs):
        g = open_group.get(s["source"])
        if g is not None:
            first, last = segs[g[0]], segs[g[-1]]
            if (0 <= s["start_ms"] - last["end_ms"] < JOIN_GAP_MS
                    and s["end_ms"] + POST_MS - (first["start_ms"] - PRE_MS) <= JOIN_MAX_MS):
                g.append(i)
                continue
        g = [i]
        open_group[s["source"]] = g
        groups.append(g)
    return groups


def assign_words(words: list[tuple[int, int, str]], lines: list[dict]) -> list[list[tuple[int, int, str]]]:
    """Each word (meeting ms) goes to the line holding its middle, or else the nearest line."""
    out: list[list[tuple[int, int, str]]] = [[] for _ in lines]
    for w in words:
        mid = (w[0] + w[1]) / 2
        best = min(range(len(lines)),
                   key=lambda k: max(lines[k]["start_ms"] - mid, mid - lines[k]["end_ms"], 0))
        out[best].append(w)
    return out


def words_text(words: list[tuple[int, int, str]]) -> str:
    return " ".join(w[2] for w in words)


class LoadWatch:
    """Samples the CPU use of everything else on the PC during a pass, so a time can be qualified."""

    def __init__(self):
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        try:
            import psutil
        except ImportError:
            return self
        me = psutil.Process()
        cores = psutil.cpu_count() or 1

        def loop():
            psutil.cpu_percent(None)
            me.cpu_percent(None)
            while not self._stop.wait(2.0):
                self.samples.append(max(0.0, psutil.cpu_percent(None) - me.cpu_percent(None) / cores))

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        return False

    @property
    def mean(self) -> float | None:
        return round(sum(self.samples) / len(self.samples), 1) if self.samples else None


def memory_mb() -> tuple[float | None, float | None]:
    """(now, peak) resident memory of this process in MB; (None, None) without psutil."""
    try:
        import psutil
    except ImportError:
        return None, None
    info = psutil.Process().memory_info()
    return round(info.rss / 2**20, 1), round(getattr(info, "peak_wset", info.rss) / 2**20, 1)


def run_tag(folder: Path, tag: str, segs: list[dict], audio: dict[str, np.ndarray], a) -> dict:
    v = variant_of(tag)
    terms = [t.strip() for t in a.vocab.split(",") if t.strip()]
    rss_before, _ = memory_mb()
    t0 = time.perf_counter()
    backend = make_backend(v, a.threads, terms, a.hotwords_score, a.models, a.out, a.bpe_scores,
                           a.hotwords_line_score)
    load_s = time.perf_counter() - t0
    usable = [s for s in segs if s["source"] in audio]
    groups = join_lines(usable, v.joined)

    def span(group: list[int]) -> tuple[int, int]:
        clip = audio[usable[group[0]]["source"]]
        i0 = max(0, (usable[group[0]]["start_ms"] - PRE_MS) * SAMPLE_RATE // 1000)
        i1 = min(len(clip), (usable[group[-1]]["end_ms"] + POST_MS) * SAMPLE_RATE // 1000)
        return i0, i1

    i0, i1 = span(groups[0])
    backend.transcribe(audio[usable[groups[0][0]]["source"]][i0:i1])  # warm-up, not timed
    rss_loaded, _ = memory_mb()
    rows: dict[int, dict] = {}
    with LoadWatch() as watch:
        t_all = time.perf_counter()
        for gid, group in enumerate(groups):
            lines = [usable[i] for i in group]
            i0, i1 = span(group)
            t0 = time.perf_counter()
            r = backend.transcribe(audio[lines[0]["source"]][i0:i1])
            took = time.perf_counter() - t0
            span_ms = [i0 * 1000 // SAMPLE_RATE, i1 * 1000 // SAMPLE_RATE]
            words = [(span_ms[0] + w0, span_ms[0] + w1, text) for w0, w1, text in r.words]
            shares = assign_words(words, lines) if v.joined else [words]
            for k, (i, s) in enumerate(zip(group, lines)):
                row = {"seq": s.get("seq"), "source": s["source"], "start_ms": s["start_ms"],
                       "end_ms": s["end_ms"], "live": s["text"],
                       "text": words_text(shares[k]) if v.joined else r.text,
                       "conf": None if r.conf is None else round(r.conf, 3),
                       "s": round(took, 3) if k == 0 else 0.0, "span_ms": span_ms,
                       "words": [list(w) for w in shares[k]]}
                if v.joined:
                    row["group"] = gid
                if r.tokens is not None and k == 0:
                    row["tokens"] = r.tokens  # of the whole span
                rows[i] = row
        total = time.perf_counter() - t_all
    _, peak = memory_mb()
    import faster_whisper
    import sherpa_onnx

    return {
        "tag": tag, "backend": v.backend, "model": v.model, "beam": v.beam, "joined": v.joined,
        "prompt": getattr(backend, "prompt", None), "hotwords": getattr(backend, "hotwords", None) or None,
        "hotwords_score": a.hotwords_score if getattr(backend, "hotwords", None) else None,
        "hotwords_line_score": a.hotwords_line_score if getattr(backend, "hotwords", None) else None,
        "bpe_scores": a.bpe_scores if getattr(backend, "hotwords", None) else None,
        "threads": a.threads, "pre_ms": PRE_MS, "post_ms": POST_MS,
        "total_s": round(total, 1), "load_s": round(load_s, 1), "spans": len(groups),
        "rss_before_mb": rss_before, "rss_loaded_mb": rss_loaded, "peak_rss_mb": peak,
        "other_cpu_pct": watch.mean, "when": datetime.now().isoformat(timespec="seconds"),
        "versions": {"faster_whisper": faster_whisper.__version__, "sherpa_onnx": sherpa_onnx.__version__},
        "rows": [rows[i] for i in sorted(rows)],
    }


def cmd_run(a) -> int:
    folder = Path(a.folder)
    a.out.mkdir(parents=True, exist_ok=True)
    if a.out.resolve().is_relative_to(folder.resolve()):
        raise SystemExit("--out must not be inside the meeting folder")
    tags = [t for t in a.tags.split(",") if t]
    for tag in tags:
        variant_of(tag)  # fail on a bad tag before any work
    segs = load_segments(folder)
    audio = {src: read_audio(folder / "audio" / f"{src}.flac") for src in ("system", "mic")
             if (folder / "audio" / f"{src}.flac").exists()}
    for tag in tags:
        dest = a.out / f"{folder.name}__{tag}.json"
        if dest.exists() and not a.force:
            print("exists", dest, flush=True)
            continue
        if a.no_lock:
            run = run_tag(folder, tag, segs, audio, a)
        else:
            with vs.HeavyLock():
                run = run_tag(folder, tag, segs, audio, a)
        dest.write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding="utf-8")
        speech = speech_s(run["rows"])
        print(f"{folder.name} {tag}: {len(run['rows'])} lines, {run['spans']} spans in {run['total_s']:.0f} s "
              f"({run['total_s'] / speech:.3f}x speech), peak {run['peak_rss_mb']} MB, "
              f"other CPU {run['other_cpu_pct']}%", flush=True)
    return 0


# -- report -------------------------------------------------------------------------------------
def speech_s(rows: list[dict]) -> float:
    return sum((r["end_ms"] - r["start_ms"]) / 1000 for r in rows) or 1e-9


def key_of(row: dict) -> tuple:
    return (row["source"], row["start_ms"], row["end_ms"])


def load_runs(out: Path) -> dict[str, dict[str, dict]]:
    runs: dict[str, dict[str, dict]] = {}
    for p in sorted(Path(out).glob("*__*.json")):
        meeting, tag = p.stem.split("__", 1)
        run = json.loads(p.read_text(encoding="utf-8"))
        run["path"] = p
        runs.setdefault(meeting, {})[tag] = run
    return runs


def pick(runs: dict[str, dict], name: str) -> str | None:
    """``name`` itself, or its run with word times (``name.w``) when there is one."""
    for tag in (f"{name}.w", name):
        if tag in runs:
            return tag
    return None


def has_words(run: dict) -> bool:
    return any(r.get("words") for r in run["rows"])


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0


def table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join("" if c is None else str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def loose_count(term: str, texts: list[str]) -> int:
    """Like ``vocab_spike.count`` but blind to spacing and hyphens inside the term, so a term the
    model heard and wrote in pieces ("ab-c d" for "AbCD") still counts. ``term`` may carry
    vocab_spike's optional plural ("codes?")."""
    parts = re.findall(r"\s+|\S\??", term)
    body = r"[\s-]*".join(p if p.endswith("?") and len(p) > 1 else re.escape(p)
                          for p in parts if not p.isspace())
    pattern = re.compile(r"(?<![\w])" + body + r"(?![\w])", re.I)
    return sum(len(pattern.findall(t)) for t in texts)


def timed_tokens(row: dict) -> list[tuple[str, int, int]]:
    """(normalised word, start ms, end ms) for a row with word times; a timed word that normalises
    to several words ("twenty-two") gives each of them its times."""
    out = []
    for w0, w1, text in row.get("words") or []:
        out.extend((tok, w0, w1) for tok in wer.normalize(text))
    return out


def tail_extra(control: list[str], words: list[str]) -> int:
    """Words on the end of ``words`` that come after the control's last word."""
    n = 0
    for op, _r, _h in reversed(wer.align(control, words)):
        if op != "ins":
            break
        n += 1
    return n


TAIL_MS_PER_WORD = 100  # nobody speaks ten words a second


def cannot_hold(row: dict, n_extra: int) -> bool:
    """True when the last ``n_extra`` words have no room in the audio: they average under
    MIN_MEAN_WORD_MS each, or start with under TAIL_MS_PER_WORD each left before the span ends."""
    toks = timed_tokens(row)
    if n_extra <= 0 or len(toks) < n_extra or len(toks) != len(wer.normalize(row["text"])):
        return False
    tail = toks[-n_extra:]
    mean_ms = sum(t[2] - t[1] for t in tail) / n_extra
    room = row["span_ms"][1] - tail[0][1] if row.get("span_ms") else None
    return mean_ms < MIN_MEAN_WORD_MS or (room is not None and room < TAIL_MS_PER_WORD * n_extra)


def line_checks(run: dict, control: dict | None) -> dict:
    """Empty and odd lines, and invented line ends, against the control run."""
    ctl = {key_of(r): wer.normalize(r["text"]) for r in control["rows"]} if control else {}
    timed = has_words(run) and not run.get("joined")  # a joined span's words are dealt out by time
    c = {"empty": 0, "short": 0, "long": 0, "late": 0 if timed else None, "tail": 0,
         "tail2": 0, "no_room": 0 if timed else None, "late_rows": [], "no_room_rows": []}
    for row in run["rows"]:
        words = wer.normalize(row["text"])
        c["empty"] += not words
        if timed and any(w[0] > row["end_ms"] + POST_MS for w in row.get("words") or []):
            c["late"] += 1
            c["late_rows"].append(row)
        base = ctl.get(key_of(row))
        if base is None or control is run:
            continue
        if base:
            c["short"] += len(words) < 0.5 * len(base)
            c["long"] += len(words) > 2 * len(base)
        extra = tail_extra(base, words) if base and words else 0
        c["tail"] += extra >= 1
        c["tail2"] += extra >= 2
        if timed and cannot_hold(row, extra):
            c["no_room"] += 1
            c["no_room_rows"].append(row)
    return c


def units(a: dict, b: dict) -> list[tuple[list[str], list[str]]]:
    """The two runs' normalised words, line by line; when one of them joined lines, by its joined
    spans (so a word that moved across a line boundary is not counted as a difference)."""
    rows_b = {key_of(r): r for r in b["rows"]}
    grouped = a if a.get("joined") else b if b.get("joined") else None
    group_of = {key_of(r): r.get("group") for r in grouped["rows"]} if grouped else {}
    out: dict = {}
    for ra in a["rows"]:
        k = key_of(ra)
        if k not in rows_b:
            continue
        unit = out.setdefault((ra["source"], group_of.get(k, k)), ([], []))
        unit[0].extend(wer.normalize(ra["text"]))
        unit[1].extend(wer.normalize(rows_b[k]["text"]))
    return list(out.values())


def difference(a: dict, b: dict) -> float:
    """Word-level difference rate: edits between the two texts over the mean of their word counts."""
    edits = na = nb = 0
    for wa, wb in units(a, b):
        edits += wer.distance(wa, wb)
        na += len(wa)
        nb += len(wb)
    return edits / max((na + nb) / 2, 1)


def word_time_offsets(run: dict, ref: dict) -> dict | None:
    """Start-time differences (run minus ref, ms) for words on lines whose normalised text is the same."""
    if not (has_words(run) and has_words(ref)):
        return None
    ref_rows = {key_of(r): r for r in ref["rows"]}
    diffs: list[int] = []
    ends: list[int] = []
    lines = 0
    for row in run["rows"]:
        other = ref_rows.get(key_of(row))
        if other is None:
            continue
        ta, tb = timed_tokens(row), timed_tokens(other)
        if not ta or [t[0] for t in ta] != [t[0] for t in tb]:
            continue
        lines += 1
        diffs.extend(x[1] - y[1] for x, y in zip(ta, tb))
        ends.extend(x[2] - y[2] for x, y in zip(ta, tb))
    if not diffs:
        return None
    mag = [abs(d) for d in diffs]
    return {"lines": lines, "words": len(diffs), "median": pct(mag, 0.5), "p90": pct(mag, 0.9),
            "worst": max(mag), "bias": pct(diffs, 0.5), "end_median": pct([abs(d) for d in ends], 0.5),
            "over_300": sum(m > 300 for m in mag)}


def report_meeting(meeting: str, runs: dict[str, dict], a) -> None:
    control_tag, ref_tag = pick(runs, a.control), pick(runs, a.ref)
    control = runs.get(control_tag) if control_tag else None
    ref = runs.get(ref_tag) if ref_tag else None
    tags = [t for t in runs if not a.tags or t in a.tags.split(",")]
    first = next(iter(runs.values()))
    print(f"\n## {meeting}\n")
    print(f"{len(first['rows'])} lines, {speech_s(first['rows']):.0f} s of speech. "
          f"Control: {control_tag}. Reference for word times and examples: {ref_tag}.\n")

    with_terms = a.terms == "all" or (a.terms == "auto" and any(r.get("prompt") or r.get("hotwords")
                                                               for r in runs.values()))
    checks = {t: line_checks(runs[t], control) for t in tags}
    rows = []
    for t in tags:
        run, c = runs[t], checks[t]
        secs = [r["s"] for r in run["rows"] if r["s"]]
        texts = [r["text"] for r in run["rows"]]
        row = [t, run["model"].replace("sherpa-onnx-nemo-", ""), run.get("beam"),
               "yes" if run.get("prompt") or run.get("hotwords") else "", run.get("threads", "default"),
               f"{run['total_s']:.0f}", f"{run['total_s'] / speech_s(run['rows']):.3f}",
               f"{pct(secs, 0.5):.2f}", f"{pct(secs, 0.9):.2f}", run.get("peak_rss_mb"), run.get("other_cpu_pct"),
               sum(len(wer.normalize(x)) for x in texts)]
        if with_terms:
            row += [sum(vs.count(e, texts) for e in vs.EXPECTED), sum(loose_count(e, texts) for e in vs.EXPECTED),
                    sum(vs.count(m, texts) for m in vs.MISHEARS)]
        rows.append(row)
    head = ["tag", "model", "beam", "vocab", "threads", "time s", "x speech", "median s", "p90 s", "peak MB",
            "other CPU %", "words"]
    print(table(head + (["terms", "terms, spelling ignored", "mishears"] if with_terms else []), rows))

    print(f"\nLines against the control ({control_tag}): empty; under half / over twice its words; a word "
          f"starting after the line's end + {POST_MS} ms; words after the control's last word (1+ / 2+); "
          "of those, with no room in the audio.\n")
    print(table(["tag", "empty", "under half", "over twice", "late word", "tail 1+", "tail 2+", "no room"],
                [[t, c["empty"], c["short"], c["long"], c["late"], c["tail"], c["tail2"], c["no_room"]]
                 for t, c in checks.items()]))

    if with_terms:
        print("\nExpected terms and known mishears, by term:\n")
        texts = {t: [r["text"] for r in runs[t]["rows"]] for t in tags}
        print("(as written / with spacing and hyphens inside the term ignored, when the two differ)\n")

        def cell(term: str, t: str) -> str:
            strict, loose = vs.count(term, texts[t]), loose_count(term, texts[t])
            return str(strict) if strict == loose else f"{strict} / {loose}"

        print(table(["term"] + tags, [[e] + [cell(e, t) for t in tags] for e in vs.EXPECTED]
                    + [[f"({m})"] + [vs.count(m, texts[t]) for t in tags] for m in vs.MISHEARS]))

    print("\nWord-level difference rate between runs (edits over the mean word count, normalised text):\n")
    print(table(["", *tags], [[x] + ["" if x == y else f"{difference(runs[x], runs[y]):.1%}" for y in tags]
                              for x in tags]))

    if ref is not None and has_words(ref):
        print(f"\nWord start times against {ref_tag}, on lines with the same normalised text (ms):\n")
        rows = []
        for t in tags:
            o = None if t == ref_tag else word_time_offsets(runs[t], ref)
            if o:
                rows.append([t, o["lines"], o["words"], o["median"], o["p90"], o["worst"], o["over_300"],
                             o["bias"], o["end_median"]])
        print(table(["tag", "lines", "words", "median", "p90", "worst", "over 300 ms", "median signed",
                     "end median"], rows))

    ref_file = wer.ref_path(meeting, a.ref_dir)
    if ref_file.exists():
        print(f"\nWord error rate against {ref_file}:\n")
        rows = []
        for t in tags:
            s, _ops = wer.score_file(runs[t]["path"], ref_file)
            rows.append([t, f"{s.wer:.1%}", s.substitutions, s.deletions, s.insertions, s.ref_words])
        print(table(["tag", "WER", "substituted", "missing", "added", "reference words"], rows))
    else:
        print(f"\nWord error rate: not measured, no reference transcript ({ref_file} does not exist).")

    if a.examples and ref is not None:
        for t in (a.example_tags.split(",") if a.example_tags else ["p"]):
            if t not in runs or t == ref_tag:
                continue
            print(f"\nExamples where {t} and {ref_tag} differ (text from the recording; keep under lab\\):\n")
            other = {key_of(r): r for r in runs[t]["rows"]}
            ctl = {key_of(r): r for r in control["rows"]} if control else {}
            vocab_run = runs.get(pick(runs, "s5v") or "")
            vrows = {key_of(r): r for r in vocab_run["rows"]} if vocab_run else {}
            differing = [r for r in ref["rows"] if key_of(r) in other
                         and wer.normalize(r["text"]) != wer.normalize(other[key_of(r)]["text"])
                         and 3 <= len(wer.normalize(r["text"])) <= 18]
            step = max(1, len(differing) // a.examples)
            for r in differing[::step][:a.examples]:
                k = key_of(r)
                print(f"- [{r['start_ms'] // 60000:02d}:{r['start_ms'] // 1000 % 60:02d} {r['source']}]")
                if k in ctl:
                    print(f"  - {control_tag}: {ctl[k]['text']}")
                print(f"  - {ref_tag}: {r['text']}")
                if k in vrows:
                    print(f"  - s5v: {vrows[k]['text']}")
                print(f"  - {t}: {other[k]['text']}")
    if a.flagged:
        for t, c in checks.items():
            for label, key in (("late word", "late_rows"), ("no room", "no_room_rows")):
                for row in c[key][:a.flagged]:
                    k = key_of(row)
                    base = next((r["text"] for r in control["rows"] if key_of(r) == k), "") if control else ""
                    print(f"\n{t} {label} [{row['start_ms'] // 1000}s {row['source']}]\n  control: {base}\n"
                          f"  {t}: {row['text']}\n  last words: {row['words'][-4:]} span {row.get('span_ms')}")


def cmd_report(a) -> int:
    runs = load_runs(a.out)
    if not runs:
        print(f"no runs in {a.out}")
        return 1
    refs = sorted(Path(a.ref_dir).glob("*.txt")) if Path(a.ref_dir).exists() else []
    print("# Re-transcription lab report\n")
    print(f"Reference transcripts in {a.ref_dir}: " + (", ".join(p.name for p in refs) if refs else
                                                      "none, so no word error rate; agreement between "
                                                      "systems is reported instead."))
    for meeting, by_tag in runs.items():
        if a.meeting and a.meeting not in meeting:
            continue
        report_meeting(meeting, by_tag, a)
    return 0


def model_cuts(run: dict) -> dict[str, list[str]]:
    """Word -> the pieces the model wrote it with, from a Parakeet run's tokens (punctuation dropped)."""
    cuts: dict[str, list[str]] = {}
    for row in run["rows"]:
        word: list[str] = []
        for tok in [*(row.get("tokens") or []), " "]:
            if tok.startswith((" ", "▁")) and word:
                while word and not any(c.isalnum() for c in word[-1]):
                    word.pop()
                if word:
                    cuts.setdefault("".join(word)[1:], word)
                word = []
            word.append("▁" + tok[1:] if tok.startswith(" ") else tok)
    return cuts


def cmd_hotwords(a) -> int:
    """Show how each vocabulary term is cut into tokens. With --check, compare the cuts with the
    pieces the model itself wrote for every word of the greedy Parakeet runs in lab/retx."""
    ranks = load_ranks(Path(a.models) / PARAKEET / "tokens.txt")
    lines = hotword_lines([t.strip() for t in a.vocab.split(",") if t.strip()])
    fitted, bad = hotword_scores(ranks, lines)
    naive = naive_scores(ranks)

    def show(line: str, cut) -> str:
        return " | ".join(" ".join(c) if c else "(cannot be cut)" for c in (cut(w) for w in line.split()))

    print(table(["hotword", "model's tokenizer (BPE merges)", "sherpa-onnx, minus-rank bpe.vocab",
                 "sherpa-onnx, fitted bpe.vocab"],
                [[line, show(line, lambda w: bpe_merge(w, ranks)), show(line, lambda w: dp_cut(w, naive)),
                  show(line, lambda w: dp_cut(w, fitted))] for line in lines]))
    if bad:
        print("\ncannot be fitted:", bad)
    if a.check:
        cuts: dict[str, list[str]] = {}
        for by_tag in load_runs(a.out).values():
            for run in by_tag.values():
                if run.get("backend") == "parakeet" and not run.get("hotwords") and run.get("beam", 1) <= 1:
                    cuts.update(model_cuts(run))
        same_merge = sum(bpe_merge(w, ranks) == cut for w, cut in cuts.items())
        same_naive = sum(dp_cut(w, naive) == cut for w, cut in cuts.items())
        print(f"\n{len(cuts)} different words written by the model in the greedy runs: BPE merges give the "
              f"model's own pieces for {same_merge} ({same_merge / max(len(cuts), 1):.1%}); the minus-rank "
              f"bpe.vocab for {same_naive} ({same_naive / max(len(cuts), 1):.1%}).")
        if a.show:
            for w, cut in cuts.items():
                if bpe_merge(w, ranks) != cut:
                    print("  differs:", w, cut, bpe_merge(w, ranks))
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="transcribe a meeting copy's lines again")
    r.add_argument("folder", type=Path)
    r.add_argument("--tags", required=True, help="comma list of " + ", ".join(VARIANTS) + " (optionally .label)")
    r.add_argument("--threads", type=int, default=4,
                   help="CPU threads for the model (0 = faster-whisper's default; Parakeet uses at least 1)")
    r.add_argument("--vocab", default=DEFAULT_TERMS, help="comma list of terms for the vocabulary variants")
    r.add_argument("--hotwords-score", type=float, default=HOTWORDS_SCORE,
                   help="sherpa-onnx hotwords_score for the Parakeet vocabulary variants")
    r.add_argument("--hotwords-line-score", type=float, default=0.0,
                   help="per-term score written into the hotwords file (0 = none)")
    r.add_argument("--bpe-scores", choices=["fitted", "naive"], default="fitted",
                   help="how the bpe.vocab for Parakeet hotwords is written (see hotword_scores)")
    r.add_argument("--out", type=Path, default=OUT)
    r.add_argument("--models", type=Path, default=ASR_DIR)
    r.add_argument("--force", action="store_true", help="overwrite an existing result")
    r.add_argument("--no-lock", action="store_true", help="do not wait for lab/heavy.lock")
    r.set_defaults(fn=cmd_run)
    p = sub.add_parser("report", help="compare the runs in lab/retx")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--meeting", default=None, help="only meetings whose name contains this")
    p.add_argument("--tags", default=None, help="only these tags")
    p.add_argument("--control", default="b1")
    p.add_argument("--ref", default="s5", help="run that word times and examples are compared with")
    p.add_argument("--terms", choices=["auto", "all", "none"], default="auto",
                   help="count vocab_spike's expected terms and mishears (auto: meetings with a vocabulary run)")
    p.add_argument("--ref-dir", type=Path, default=wer.REF_DIR)
    p.add_argument("--examples", type=int, default=0, help="print N lines where a run and --ref differ")
    p.add_argument("--example-tags", default=None)
    p.add_argument("--flagged", type=int, default=0, help="print up to N flagged line ends per run")
    p.set_defaults(fn=cmd_report)
    h = sub.add_parser("hotwords", help="show how the vocabulary is cut into Parakeet tokens")
    h.add_argument("--vocab", default=DEFAULT_TERMS)
    h.add_argument("--models", type=Path, default=ASR_DIR)
    h.add_argument("--out", type=Path, default=OUT)
    h.add_argument("--check", action="store_true", help="compare cuts with the model's own tokens in lab/retx")
    h.add_argument("--show", action="store_true", help="with --check: print the words that differ (lab only)")
    h.set_defaults(fn=cmd_hotwords)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
