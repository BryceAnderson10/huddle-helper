"""After-Stop pass (F8): refined speaker labels and transcript.final.jsonl.

Steps, in the order the output contract requires:
1. diarize the buffered audio of each diarized stream,
2. relabel live segments (splitting a segment where the speaker changes
   mid-utterance, using word timings),
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
from .diarize import MIC_PREFIX, REMOTE, SYSTEM_PREFIX, YOU, OfflineDiarizer, Turn, label
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


def _split_by_words(seg: Segment, words: list[Word], turns: list[Turn]) -> list[tuple[int | None, list[Word]]]:
    speakers = [speaker_at(w.start_ms, w.end_ms, turns) for w in words]
    # Words with no nearby turn inherit their neighbour's speaker.
    for i in range(len(speakers)):
        if speakers[i] is None and i > 0:
            speakers[i] = speakers[i - 1]
    for i in range(len(speakers) - 2, -1, -1):
        if speakers[i] is None:
            speakers[i] = speakers[i + 1]

    runs: list[tuple[int | None, list[Word]]] = []
    for spk, w in zip(speakers, words):
        if runs and runs[-1][0] == spk:
            runs[-1][1].append(w)
        else:
            runs.append((spk, [w]))
    # Single-word runs are usually boundary jitter: fold them into a neighbour.
    if len(runs) > 1:
        merged: list[tuple[int | None, list[Word]]] = []
        for i, (spk, ws) in enumerate(runs):
            if len(ws) == 1 and len(runs) > 1:
                if merged:
                    merged[-1][1].extend(ws)
                    continue
                nxt = runs[i + 1]
                runs[i + 1] = (nxt[0], ws + nxt[1])
                continue
            if merged and merged[-1][0] == spk:
                merged[-1][1].extend(ws)
            else:
                merged.append((spk, list(ws)))
        runs = merged or runs
    return runs


def relabel(segments: list[Segment], words: dict[tuple[str, int], list[Word]],
            turns_by_source: dict[str, list[Turn] | None], diarized: set[str]) -> list[Segment]:
    """Apply refined speaker labels. ``turns_by_source[src] is None`` keeps live labels."""
    out: list[Segment] = []
    for seg in segments:
        if seg.source not in diarized:
            out.append(Segment(seg.source, seg.start_ms, seg.end_ms, seg.text, seg.confidence,
                               UNDIARIZED[seg.source]))
            continue
        turns = turns_by_source.get(seg.source)
        if not turns:
            out.append(seg)
            continue
        seg_words = words.get((seg.source, seg.start_ms))
        if seg_words:
            for spk, ws in _split_by_words(seg, seg_words, turns):
                text = "".join(w.text for w in ws).strip()
                if not text:
                    continue
                out.append(Segment(seg.source, ws[0].start_ms, ws[-1].end_ms, text, seg.confidence,
                                   f"#{spk}" if spk is not None else seg.speaker))
        else:
            spk = speaker_at(seg.start_ms, seg.end_ms, turns)
            out.append(Segment(seg.source, seg.start_ms, seg.end_ms, seg.text, seg.confidence,
                               f"#{spk}" if spk is not None else seg.speaker))

    # Number refined speakers per stream in order of first appearance.
    names: dict[tuple[str, str], str] = {}
    counters = {"mic": 0, "system": 0}
    for seg in out:
        if seg.speaker.startswith("#"):
            key = (seg.source, seg.speaker)
            if key not in names:
                counters[seg.source] += 1
                names[key] = label(PREFIX[seg.source], counters[seg.source])
            seg.speaker = names[key]
    out.sort(key=lambda s: (s.start_ms, s.source))
    return out


# -- the full pass -------------------------------------------------------
def finalize_meeting(folder: mt.MeetingFolder, diarizer_factory: Callable[[], OfflineDiarizer] | None,
                     keep_audio: bool, progress: Progress | None = None) -> int:
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
    diarizer = None
    for i, src in enumerate(sorted(diarized)):
        audio_path = folder.audio_file(src)
        if not audio_path.exists():
            log.warning("No buffered %s audio for %s; keeping live labels", src, folder.meeting_id)
            turns_by_source[src] = None
            continue
        try:
            if diarizer is None:
                if diarizer_factory is None:
                    raise RuntimeError("no diarizer available")
                diarizer = diarizer_factory()
            progress(f"Identifying speakers ({src})", 0.0)
            audio = read_audio(audio_path)
            turns_by_source[src] = diarizer.diarize(
                audio, lambda f, s=src: progress(f"Identifying speakers ({s})", f))
            del audio
        except Exception:  # noqa: BLE001 - fall back to live labels rather than lose the meeting
            log.exception("Refined diarization failed for %s; keeping live labels", src)
            turns_by_source[src] = None

    progress("Writing final transcript", 1.0)
    final_segments = relabel(segments, words, turns_by_source, diarized)
    count = write_transcript(folder.final_tmp_path, folder.meeting_id, started_at, ended_at, final_segments)
    os.replace(folder.final_tmp_path, folder.final_path)

    if not keep_audio and folder.audio_dir.exists():
        shutil.rmtree(folder.audio_dir, ignore_errors=True)

    folder.update_meta(status=mt.STATUS_COMPLETE, final_written_at=mt.iso(mt.now_local()),
                       final_segments=count, audio_kept=bool(keep_audio and folder.audio_dir.exists()),
                       refined_streams=sorted(s for s, t in turns_by_source.items() if t))
    folder.mark_done()
    return count
