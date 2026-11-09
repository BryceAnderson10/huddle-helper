"""After-Stop pass (F8): refined speaker labels and transcript.final.jsonl.

Steps, in the order the output contract requires:
1. diarize the buffered audio of each diarized stream (used to find where a
   line should be split because the speaker changes mid-line),
2. label every line or piece by clustering voice fingerprints over the whole
   meeting, then join sentences that were cut into lines at pauses,
3. write transcript.final.jsonl under a temp name and rename it into place,
4. delete the buffered audio unless Keep audio is on,
5. mark meta.json complete and create DONE last.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Callable

from . import meeting as mt
from .audio.buffer import read_audio
import numpy as np

from .audio import SAMPLE_RATE
from .diarize import (MIC_PREFIX, REMOTE, SYSTEM_PREFIX, YOU, Embedder, OfflineDiarizer, Turn,
                      cluster_voices, fold_small_clusters, label)
from .segments import Segment, Word
from .writer import read_events, write_transcript

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]
PREFIX = {"mic": MIC_PREFIX, "system": SYSTEM_PREFIX}
UNDIARIZED = {"mic": YOU, "system": REMOTE}


# -- word timings sidecar ------------------------------------------------
def append_words(path, segment: Segment) -> None:
    if not segment.words:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"source": segment.source, "start_ms": segment.start_ms,
                             "words": [w.to_list() for w in segment.words]}) + "\n")


def load_words(path) -> dict[tuple[str, int], list[Word]]:
    out: dict[tuple[str, int], list[Word]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                    out[(item["source"], int(item["start_ms"]))] = [Word.from_list(w) for w in item["words"]]
                except (ValueError, KeyError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


# -- relabeling ----------------------------------------------------------
def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def speaker_at(start: int, end: int, turns: list[Turn], max_gap_ms: int = 2000) -> int | None:
    """Speaker with the most overlap with [start, end]; else the nearest turn."""
    best, best_ov = None, 0
    for t in turns:
        if t.start_ms > end:
            break
        ov = _overlap(start, end, t.start_ms, t.end_ms)
        if ov > best_ov:
            best, best_ov = t.speaker, ov
    if best is not None:
        return best
    mid = (start + end) / 2
    nearest = min(turns, key=lambda t: min(abs(mid - t.start_ms), abs(mid - t.end_ms)), default=None)
    if nearest is not None and min(abs(mid - nearest.start_ms), abs(mid - nearest.end_ms)) <= max_gap_ms:
        return nearest.speaker
    return None


PHRASE_PAUSE_MS = 300  # a gap this long between words is a place a speaker could change
MIN_RUN_MS = 1000  # shorter same-speaker pieces are treated as boundary jitter
MIN_RUN_WORDS = 3
_SENTENCE_END = (".", "?", "!")

Run = tuple[int | None, list[Word]]


def _phrases(words: list[Word]) -> list[list[Word]]:
    """Split words at sentence ends and pauses: the only places a split is allowed."""
    phrases: list[list[Word]] = [[]]
    for i, w in enumerate(words):
        phrases[-1].append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is not None and (w.text.strip().endswith(_SENTENCE_END)
                                or nxt.start_ms - w.end_ms >= PHRASE_PAUSE_MS):
            phrases.append([])
    return [p for p in phrases if p]


def _phrase_speaker(phrase: list[Word], turns: list[Turn]) -> int | None:
    """Speaker with the most overlap across the phrase's words; else the nearest turn."""
    totals: dict[int, int] = {}
    for w in phrase:
        for t in turns:
            if t.start_ms > w.end_ms:
                break
            ov = _overlap(w.start_ms, w.end_ms, t.start_ms, t.end_ms)
            if ov:
                totals[t.speaker] = totals.get(t.speaker, 0) + ov
    if totals:
        return max(totals, key=totals.get)
    return speaker_at(phrase[0].start_ms, phrase[-1].end_ms, turns)


def _is_short(run: Run) -> bool:
    ws = run[1]
    return ws[-1].end_ms - ws[0].start_ms < MIN_RUN_MS or len(ws) < MIN_RUN_WORDS


def _join_same(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for spk, ws in runs:
        if out and out[-1][0] == spk:
            out[-1][1].extend(ws)
        else:
            out.append((spk, list(ws)))
    return out


def _split_by_words(seg: Segment, words: list[Word], turns: list[Turn]) -> list[Run]:
    runs: list[Run] = [(_phrase_speaker(p, turns), p) for p in _phrases(words)]
    # Phrases with no nearby turn inherit their neighbour's speaker.
    for i in range(1, len(runs)):
        if runs[i][0] is None:
            runs[i] = (runs[i - 1][0], runs[i][1])
    for i in range(len(runs) - 2, -1, -1):
        if runs[i][0] is None:
            runs[i] = (runs[i + 1][0], runs[i][1])
    runs = _join_same(runs)

    # Fold short pieces into a neighbour, shortest first, until none are left.
    while len(runs) > 1:
        short = [i for i, r in enumerate(runs) if _is_short(r)]
        if not short:
            break
        i = min(short, key=lambda k: runs[k][1][-1].end_ms - runs[k][1][0].start_ms)
        target = i - 1 if i > 0 else i + 1
        spk = runs[target][0]
        runs[i] = (spk, runs[i][1])
        runs = _join_same(runs)
    return runs


Embed = Callable[[str, int, int], "np.ndarray | None"]  # (source, start_ms, end_ms) -> fingerprint


def _voice_labels(pieces: list[tuple[int, Segment]], embed: Embed, threshold: float) -> None:
    """Label one stream's pieces by clustering their voices over the whole meeting.

    ``pieces`` are (line index, segment) pairs; labels are written to segment.speaker as
    "#v<cluster>". Pieces too short to fingerprint take a neighbour's label.
    """
    vecs, weights, idx = [], [], []
    for i, (_line, seg) in enumerate(pieces):
        vec = embed(seg.source, seg.start_ms, seg.end_ms)
        if vec is not None:
            vecs.append(vec)
            weights.append(max((seg.end_ms - seg.start_ms) / 1000, 0.1))
            idx.append(i)
    if not vecs:
        return
    labels = fold_small_clusters(cluster_voices(vecs, weights, threshold), vecs, weights)
    assigned: dict[int, int] = dict(zip(idx, labels))
    for i, (line, seg) in enumerate(pieces):
        if i in assigned:
            continue
        # Prefer another piece of the same line, else the nearest piece in time.
        same_line = [j for j in assigned if pieces[j][0] == line]
        pool = same_line or list(assigned)
        nearest = min(pool, key=lambda j: abs(pieces[j][1].start_ms - seg.start_ms))
        assigned[i] = assigned[nearest]
    for i, (_line, seg) in enumerate(pieces):
        seg.speaker = f"#v{assigned[i]}"


def _rejoin_lines(pieces: list[tuple[int, Segment]], words: dict[tuple[str, int], list[Word]]) -> list[Segment]:
    """Put back together pieces of one live line that ended up with the same speaker."""
    out: list[tuple[int, Segment]] = []
    for line, seg in pieces:
        if out and out[-1][0] == line and out[-1][1].speaker == seg.speaker:
            prev = out[-1][1]
            out[-1] = (line, Segment(prev.source, prev.start_ms, seg.end_ms, f"{prev.text} {seg.text}",
                                     prev.confidence, prev.speaker, prev.words + seg.words))
        else:
            out.append((line, seg))
    return [seg for _line, seg in out]


def relabel(segments: list[Segment], words: dict[tuple[str, int], list[Word]],
            turns_by_source: dict[str, list[Turn] | None], diarized: set[str],
            embed: Embed | None = None, voice_threshold: float = 0.65) -> list[Segment]:
    """Apply refined speaker labels.

    With ``embed`` (the buffered audio is available), each line is split where the offline
    diarizer saw a speaker change at a phrase boundary, and every piece is then labelled by
    whole-meeting voice clustering (``cluster_voices``). Without it, the diarizer's own
    speaker ids are used. ``turns_by_source[src] is None`` and no ``embed`` keeps live labels.
    """
    out: list[Segment] = []
    pieces_by_source: dict[str, list[tuple[int, Segment]]] = {}
    for line_no, seg in enumerate(segments):
        if seg.source not in diarized:
            out.append(Segment(seg.source, seg.start_ms, seg.end_ms, seg.text, seg.confidence,
                               UNDIARIZED[seg.source]))
            continue
        turns = turns_by_source.get(seg.source)
        if not turns and embed is None:
            out.append(seg)
            continue
        seg_words = words.get((seg.source, seg.start_ms))
        pieces: list[Segment] = []
        if seg_words and turns:
            for spk, ws in _split_by_words(seg, seg_words, turns):
                text = "".join(w.text for w in ws).strip()
                if text:
                    pieces.append(Segment(seg.source, ws[0].start_ms, ws[-1].end_ms, text, seg.confidence,
                                          f"#{spk}" if spk is not None else seg.speaker, list(ws)))
        else:
            spk = speaker_at(seg.start_ms, seg.end_ms, turns) if turns else None
            pieces.append(Segment(seg.source, seg.start_ms, seg.end_ms, seg.text, seg.confidence,
                                  f"#{spk}" if spk is not None else seg.speaker, seg_words or []))
        if embed is None:
            out.extend(pieces)
        else:
            pieces_by_source.setdefault(seg.source, []).extend((line_no, p) for p in pieces)

    for source, pieces in pieces_by_source.items():
        try:
            _voice_labels(pieces, embed, voice_threshold)
        except Exception:  # noqa: BLE001 - fall back to the diarizer's labels
            log.exception("Voice clustering failed for %s; using diarizer labels", source)
        out.extend(_rejoin_lines(pieces, words))

    # Number refined speakers per stream in order of first appearance.
    out.sort(key=lambda s: (s.start_ms, s.source))
    names: dict[tuple[str, str], str] = {}
    counters = {"mic": 0, "system": 0}
    for seg in out:
        if seg.speaker.startswith("#"):
            key = (seg.source, seg.speaker)
            if key not in names:
                counters[seg.source] += 1
                names[key] = label(PREFIX[seg.source], counters[seg.source])
            seg.speaker = names[key]
    return out


JOIN_GAP_MS = 2000  # Test #3's "...test number three." / "of Huddle Helper." had a 1.6 s pause
JOIN_MAX_MS = 30_000
_CLOSERS = (".", "?", "!")


def join_fragments(segments: list[Segment]) -> list[Segment]:
    """Join a sentence that was cut into lines at natural pauses.

    Test #3: "...initiate test number three." / "of Huddle Helper." and "we're going to close
    out." / "the" / "Test number three." Whisper puts a full stop on every piece. Two lines
    next to each other in the transcript are joined when they have the same source and
    speaker, the gap is short, and either the next starts in lower case or the previous has
    no closing punctuation. Only the final file is changed; the live file is append-only.
    """
    out: list[Segment] = []
    for seg in segments:
        prev = out[-1] if out else None
        if (prev is not None and prev.source == seg.source and prev.speaker == seg.speaker
                and 0 <= seg.start_ms - prev.end_ms < JOIN_GAP_MS
                and seg.end_ms - prev.start_ms <= JOIN_MAX_MS and seg.text):
            continues = seg.text[0].islower()
            if continues or not prev.text.rstrip().endswith(_CLOSERS):
                head = prev.text.rstrip()
                if continues and head.endswith(".") and not head.endswith("..."):
                    head = head[:-1]  # Whisper's full stop at the pause, not a real sentence end
                dur_p, dur_s = prev.end_ms - prev.start_ms, seg.end_ms - seg.start_ms
                conf = (prev.confidence * dur_p + seg.confidence * dur_s) / max(dur_p + dur_s, 1)
                out[-1] = Segment(prev.source, prev.start_ms, seg.end_ms, f"{head} {seg.text.lstrip()}",
                                  conf, prev.speaker, prev.words + seg.words)
                continue
        out.append(seg)
    return out


# -- the full pass -------------------------------------------------------
def finalize_meeting(folder: mt.MeetingFolder, diarizer_factory: Callable[[], OfflineDiarizer] | None,
                     keep_audio: bool, progress: Progress | None = None,
                     embedder_factory: Callable[[], Embedder] | None = None,
                     voice_threshold: float = 0.65) -> int:
    """Write transcript.final.jsonl, clean up audio and create DONE. Returns segment count."""
    progress = progress or (lambda stage, frac: None)
    meta = folder.read_meta()
    folder.update_meta(status=mt.STATUS_PROCESSING)

    events = read_events(folder.live_path) if folder.live_path.exists() else []
    segments = [Segment.from_event(e) for e in events if e.get("type") == "segment"]
    start_event = next((e for e in events if e.get("type") == "meeting_start"), None)
    end_event = next((e for e in reversed(events) if e.get("type") == "meeting_end"), None)
    started_at = start_event["started_at"] if start_event else meta.get("started_at")
    ended_at = (end_event or {}).get("ended_at") or meta.get("ended_at") or mt.iso(mt.now_local())

    rec = meta.get("settings", {})
    diarized = {src for src, on in (("system", rec.get("diarize_system", True)),
                                    ("mic", rec.get("diarize_mic", False))) if on}
    words = load_words(folder.words_path)

    turns_by_source: dict[str, list[Turn] | None] = {}
    audio_by_source: dict[str, np.ndarray] = {}
    diarizer = None
    for i, src in enumerate(sorted(diarized)):
        audio_path = folder.audio_file(src)
        if not audio_path.exists():
            log.warning("No buffered %s audio for %s; keeping live labels", src, folder.meeting_id)
            turns_by_source[src] = None
            continue
        try:
            audio_by_source[src] = read_audio(audio_path)
        except Exception:  # noqa: BLE001
            log.exception("Could not read buffered %s audio; keeping live labels", src)
            turns_by_source[src] = None
            continue
        try:
            if diarizer is None:
                if diarizer_factory is None:
                    raise RuntimeError("no diarizer available")
                diarizer = diarizer_factory()
            progress(f"Identifying speakers ({src})", 0.0)
            turns_by_source[src] = diarizer.diarize(
                audio_by_source[src], lambda f, s=src: progress(f"Identifying speakers ({s})", f))
        except Exception:  # noqa: BLE001 - fall back rather than lose the meeting
            log.exception("Refined diarization failed for %s", src)
            turns_by_source[src] = None

    embed = None
    if embedder_factory is not None and audio_by_source:
        try:
            embedder = embedder_factory()

            def embed(source: str, start_ms: int, end_ms: int):
                audio = audio_by_source.get(source)
                if audio is None:
                    return None
                return embedder.embed(audio[start_ms * SAMPLE_RATE // 1000:end_ms * SAMPLE_RATE // 1000])
        except Exception:  # noqa: BLE001
            log.exception("Voice fingerprints unavailable; using diarizer labels")
            embed = None

    progress("Grouping voices", 1.0)
    final_segments = relabel(segments, words, turns_by_source, diarized, embed, voice_threshold)
    final_segments = join_fragments(final_segments)
    refined = sorted(s for s in diarized
                     if s in audio_by_source and (embed is not None or turns_by_source.get(s)))
    audio_by_source.clear()
    progress("Writing final transcript", 1.0)
    count = write_transcript(folder.final_tmp_path, folder.meeting_id, started_at, ended_at, final_segments)
    os.replace(folder.final_tmp_path, folder.final_path)

    if not keep_audio and folder.audio_dir.exists():
        shutil.rmtree(folder.audio_dir, ignore_errors=True)

    folder.update_meta(status=mt.STATUS_COMPLETE, final_written_at=mt.iso(mt.now_local()),
                       final_segments=count, audio_kept=bool(keep_audio and folder.audio_dir.exists()),
                       refined_streams=refined)
    folder.mark_done()
    return count
