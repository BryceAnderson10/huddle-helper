"""After-Stop pass (F8): refined speaker labels and transcript.final.jsonl.

Steps, in the order the output contract requires:
1. diarize the buffered audio of each diarized stream (used to find where a
   line should be split because the speaker changes mid-line),
2. label every line or piece by clustering voice fingerprints over the whole
   meeting, then join sentences that were cut into lines at pauses,
3. write transcript.final.jsonl under a temp name and rename it into place, stamped with a new
   transcript version (its first line, and meta.json at step 5),
4. delete the buffered audio unless Keep audio is on,
5. mark meta.json complete and create DONE last.

Names (WP8): when the meeting has ``speaker_tags.jsonl``, the tags are read before clustering and become
rules (pieces the user named differently never share a speaker, and a cluster holding a name is never
folded into an unnamed or differently named one), and read again once the final file is in place to name the final speakers. The order at
the end is then: final file -> speakers.json -> audio removed -> meta.json complete -> DONE. With no tags
file nothing changes, byte for byte.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
import os
import shutil
import time
from typing import Callable

from . import meeting as mt, tags as tags_mod, vocab as vocab_mod
from .audio.buffer import read_audio
import numpy as np

from .audio import SAMPLE_RATE
from .diarize import (MIC_PREFIX, REMOTE, SYSTEM_PREFIX, YOU, Embedder, OfflineDiarizer, Turn,
                      VoiceProfile, cluster_voices, fold_small_clusters, label, profile)
from .retranscribe import FinalTranscriber, retranscribe
from .segments import Segment, Word
from .writer import new_transcript_version, read_events, write_transcript

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


Pieces = list[tuple[int, Segment]]  # (live line index, piece)


def embed_pieces(pieces: Pieces, embed: Embed, progress: Callable[[float], None] | None = None
                 ) -> tuple[list[np.ndarray], list[float], list[int]]:
    """Fingerprint each piece. Returns (vectors, weights in seconds, index of each vector's piece).

    Pieces too short to fingerprint are left out of all three lists. ``progress`` gets the
    fraction of pieces done after each one.
    """
    vecs, weights, idx = [], [], []
    for i, (_line, seg) in enumerate(pieces):
        if progress:
            progress(i / len(pieces))
        vec = embed(seg.source, seg.start_ms, seg.end_ms)
        if vec is not None:
            vecs.append(vec)
            weights.append(max((seg.end_ms - seg.start_ms) / 1000, 0.1))
            idx.append(i)
    if progress:
        progress(1.0)
    return vecs, weights, idx


def label_voices(vecs: list[np.ndarray], weights: list[float], threshold: float,
                 names: list[str | None] | None = None, fold_min: float | None = None) -> list[int]:
    """Cluster voice fingerprints over the whole meeting, then fold clusters with very little speech.

    ``names`` (one per vector, None for an unnamed piece) are the user's rules: differently named
    pieces never share a cluster, and a cluster holding a name is never folded into one that is unnamed
    or has another name (it may fold into a larger cluster with the same name). ``fold_min`` is how alike
    a small cluster must be to fold; None means the same as ``threshold``."""
    fold_min = threshold if fold_min is None else fold_min
    if names is None or not any(names):
        return fold_small_clusters(cluster_voices(vecs, weights, threshold), vecs, weights, min_similarity=fold_min)
    labels = cluster_voices(vecs, weights, threshold, names)
    cluster_names = {labels[k]: n for k, n in enumerate(names) if n}  # at most one name per cluster
    return fold_small_clusters(labels, vecs, weights, min_similarity=fold_min, protected=set(cluster_names),
                               cluster_names=cluster_names)


def assign_labels(pieces: Pieces, idx: list[int], labels: list[int]) -> None:
    """Write "#v<cluster>" to each piece's speaker; unfingerprinted pieces take a neighbour's label."""
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


def _voice_labels(pieces: Pieces, embed: Embed, voice: VoiceProfile,
                  timings: dict[str, float] | None = None,
                  progress: Callable[[float], None] | None = None,
                  ranges: list[tags_mod.TaggedRange] | None = None) -> None:
    """Label one stream's pieces by clustering their voices over the whole meeting.

    Labels are written to segment.speaker as "#v<cluster>". ``timings`` collects seconds
    under "embed_pieces" and "cluster".
    """
    t0 = time.perf_counter()
    try:
        vecs, weights, idx = embed_pieces(pieces, embed, progress)
    finally:
        if timings is not None:
            timings["embed_pieces"] = timings.get("embed_pieces", 0.0) + time.perf_counter() - t0
    if not vecs:
        return
    t0 = time.perf_counter()
    try:
        names = tags_mod.piece_names(pieces, idx, vecs, weights, ranges, voice.tag_gate) if ranges else None
        assign_labels(pieces, idx, label_voices(vecs, weights, voice.final_match, names, voice.fold_min))
    finally:
        if timings is not None:
            timings["cluster"] = timings.get("cluster", 0.0) + time.perf_counter() - t0


def _rejoin_lines(pieces: Pieces, words: dict[tuple[str, int], list[Word]]) -> list[Segment]:
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


def split_pieces(segments: list[Segment], words: dict[tuple[str, int], list[Word]],
                 turns_by_source: dict[str, list[Turn] | None], diarized: set[str],
                 embed_available: bool) -> tuple[list[Segment], dict[str, Pieces]]:
    """Split lines where the diarizer saw a speaker change, before voice labelling.

    Returns (segments that are final already, pieces per source waiting for voice labels).
    Pieces are only collected when ``embed_available``; otherwise they are in the first list.
    """
    out: list[Segment] = []
    pieces_by_source: dict[str, Pieces] = {}
    for line_no, seg in enumerate(segments):
        if seg.source not in diarized:
            out.append(Segment(seg.source, seg.start_ms, seg.end_ms, seg.text, seg.confidence,
                               UNDIARIZED[seg.source]))
            continue
        turns = turns_by_source.get(seg.source)
        if not turns and not embed_available:
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
        if not embed_available:
            out.extend(pieces)
        else:
            pieces_by_source.setdefault(seg.source, []).extend((line_no, p) for p in pieces)
    return out, pieces_by_source


def relabel(segments: list[Segment], words: dict[tuple[str, int], list[Word]],
            turns_by_source: dict[str, list[Turn] | None], diarized: set[str],
            embed: Embed | None = None, voice_threshold: float | None = None,
            timings: dict[str, float] | None = None,
            progress: Callable[[float], None] | None = None,
            ranges: list[tags_mod.TaggedRange] | None = None,
            voice: VoiceProfile | None = None) -> list[Segment]:
    """Apply refined speaker labels.

    With ``embed`` (the buffered audio is available), each line is split where the offline
    diarizer saw a speaker change at a phrase boundary, and every piece is then labelled by
    whole-meeting voice clustering (``cluster_voices``). Without it, the diarizer's own
    speaker ids are used. ``turns_by_source[src] is None`` and no ``embed`` keeps live labels.
    ``timings`` (optional) collects seconds under "embed_pieces" and "cluster"; ``progress``
    (optional) gets the fraction of all pieces fingerprinted. ``ranges`` (optional) are the live lines
    the user named (``tags.tagged_ranges``); they steer the clustering (L14). ``voice`` holds the
    fingerprint model's cutoffs (default: the shipped model's); a ``voice_threshold`` replaces its
    ``final_match``.
    """
    voice = voice or profile()
    if voice_threshold is not None:
        voice = replace(voice, final_match=voice_threshold)
    out, pieces_by_source = split_pieces(segments, words, turns_by_source, diarized, embed is not None)
    total = sum(len(p) for p in pieces_by_source.values()) or 1
    done = 0

    for source, pieces in pieces_by_source.items():
        def part(f: float, base: int = done, n: int = len(pieces)) -> None:
            if progress:
                progress((base + f * n) / total)

        done += len(pieces)
        try:
            _voice_labels(pieces, embed, voice, timings, part, ranges)
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
ATTACH_GAP_MS = 1000  # Test #4: a sentence end cut off onto another speaker came within a second
ATTACH_MAX_WORDS = 3  # ...and was too short to fingerprint, so it took a neighbour's label
_CLOSERS = (".", "?", "!")
_ELLIPSES = ("…", "...")


def _is_closed(text: str) -> bool:
    """True if the line ends a sentence. A trailing "…" or "..." is Whisper trailing off (Test #4)."""
    text = text.rstrip()
    return text.endswith(_CLOSERS) and not text.endswith(_ELLIPSES)


def _should_join(prev: Segment, seg: Segment) -> bool:
    gap = seg.start_ms - prev.end_ms
    if prev.source != seg.source or gap < 0 or seg.end_ms - prev.start_ms > JOIN_MAX_MS or not seg.text:
        return False
    if prev.speaker == seg.speaker:
        return gap < JOIN_GAP_MS and (seg.text[0].islower() or not _is_closed(prev.text))
    # Cross-speaker: a very short line right after one that stops mid-sentence is its tail, whoever
    # it was labelled as. A line that trails off ("Yeah, so…") doesn't count: on Test #4 another
    # person's short reply ("Cool. Okay.") follows those, and attaching it would misattribute it.
    return (gap < ATTACH_GAP_MS and not prev.text.rstrip().endswith(_CLOSERS + _ELLIPSES)
            and len(seg.text.split()) <= ATTACH_MAX_WORDS)


def join_fragments(segments: list[Segment]) -> list[Segment]:
    """Join a sentence that was cut into lines at natural pauses.

    Test #3: "...initiate test number three." / "of Huddle Helper." and "we're going to close
    out." / "the" / "Test number three." Whisper puts a full stop on every piece. Two lines
    next to each other in the transcript are joined when they have the same source and
    speaker, the gap is short, and either the next starts in lower case or the previous has
    no closing punctuation (a trailing "…" is not closing punctuation). Test #4: a line of
    three words or fewer less than a second after a line with no ending punctuation at all is
    attached to it even when the speaker label differs; the earlier line's speaker is kept. Only the final file
    is changed; the live file is append-only.
    """
    out: list[Segment] = []
    for seg in segments:
        prev = out[-1] if out else None
        if prev is not None and _should_join(prev, seg):
            head = prev.text.rstrip()
            if seg.text[0].islower() and head.endswith(".") and not head.endswith(_ELLIPSES):
                head = head[:-1]  # Whisper's full stop at the pause, not a real sentence end
            dur_p, dur_s = prev.end_ms - prev.start_ms, seg.end_ms - seg.start_ms
            conf = (prev.confidence * dur_p + seg.confidence * dur_s) / max(dur_p + dur_s, 1)
            out[-1] = Segment(prev.source, prev.start_ms, seg.end_ms, f"{head} {seg.text.lstrip()}",
                              conf, prev.speaker, prev.words + seg.words)
            continue
        out.append(seg)
    return out


def read_tags(folder: mt.MeetingFolder) -> tags_mod.TagState:
    """The user's names from speaker_tags.jsonl (empty when there is no file or it cannot be read)."""
    if not folder.tags_path.exists():
        return tags_mod.TagState()
    try:
        return tags_mod.reduce(read_events(folder.tags_path))
    except (OSError, ValueError):
        log.exception("Could not read %s", folder.tags_path)
        return tags_mod.TagState()


def write_names(folder: mt.MeetingFolder, live_segments: list[Segment], final_segments: list[Segment]) -> bool:
    """Name the final speakers from the tags as they are now (not as they were when the pass began),
    and write speakers.json. Does nothing, and writes nothing, when nobody was named."""
    ranges = tags_mod.tagged_ranges(read_tags(folder), live_segments)
    if not ranges:
        return False
    mapping, conflicts = tags_mod.map_final_speakers(final_segments, ranges)
    folder.merge_speakers(mapping, from_tags=True, conflicts=conflicts)
    for c in conflicts:
        log.warning("Speaker %s: the final pass put %s together", c["speaker"], " and ".join(c["names"]))
    return True


def _remove_audio(folder: mt.MeetingFolder, keep_audio: bool) -> None:
    """Delete the audio folder unless Keep audio is on, re-reading meta.json so a late change is honoured."""
    if not folder.audio_dir.exists():
        return
    try:
        keep_audio = keep_audio or bool(folder.read_meta().get("settings", {}).get("keep_audio"))
    except (OSError, ValueError):
        pass
    if keep_audio:
        return
    shutil.rmtree(folder.audio_dir, ignore_errors=True)
    if folder.audio_dir.exists():
        time.sleep(1.0)  # a file may still be open (antivirus, indexer)
        shutil.rmtree(folder.audio_dir, ignore_errors=True)
        if folder.audio_dir.exists():
            log.warning("Could not remove all buffered audio in %s", folder.audio_dir)


# -- the full pass -------------------------------------------------------
def finalize_meeting(folder: mt.MeetingFolder, diarizer_factory: Callable[[], OfflineDiarizer] | None,
                     keep_audio: bool, progress: Progress | None = None,
                     embedder_factory: Callable[[], Embedder] | None = None,
                     voice_threshold: float | None = None, voice: VoiceProfile | None = None,
                     transcriber_factory: Callable[[], FinalTranscriber] | None = None) -> int:
    """Write transcript.final.jsonl, clean up audio and create DONE. Returns segment count.

    With ``transcriber_factory`` the kept audio of both sources is transcribed a second time first
    (``retranscribe``); if that fails for any reason the pass carries on with the live text."""
    progress = progress or (lambda stage, frac: None)
    t_start = time.perf_counter()
    stages: dict[str, float] = {}
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
                                    ("mic", mt.has_shared_mic(meta))) if on}
    words = load_words(folder.words_path)

    preloaded: dict[str, np.ndarray] = {}
    retx: dict = {}
    if transcriber_factory is not None and segments:
        t0 = time.perf_counter()
        try:
            progress("Improving the transcript", 0.0)
            for src in sorted({s.source for s in segments}):
                if folder.audio_file(src).exists():
                    preloaded[src] = read_audio(folder.audio_file(src))
            if preloaded:
                transcriber = transcriber_factory()
                terms = [t for t in meta.get("vocabulary") or [] if isinstance(t, str)]
                segments, words, rstats = retranscribe(
                    segments, words, preloaded, transcriber, vocab_mod.build_prompt(terms),
                    lambda f: progress("Improving the transcript", f))
                retx = {"final_transcriber": getattr(transcriber, "name", "unknown"),
                        "retx_lines_changed": rstats.changed, "retx_lines_kept_live": rstats.kept_live}
                del transcriber
        except Exception:  # noqa: BLE001 - the live text is a complete transcript; never lose the meeting
            log.exception("Second transcription failed for %s; keeping the live text", folder.meeting_id)
            retx = {"final_transcriber": None, "retx_failed": True}
        stages["retranscribe"] = time.perf_counter() - t0

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
            t0 = time.perf_counter()
            audio_by_source[src] = preloaded[src] if src in preloaded else read_audio(audio_path)
            stages["read_audio"] = stages.get("read_audio", 0.0) + time.perf_counter() - t0
        except Exception:  # noqa: BLE001
            log.exception("Could not read buffered %s audio; keeping live labels", src)
            turns_by_source[src] = None
            continue
        t0 = time.perf_counter()
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
        took_src = time.perf_counter() - t0
        merge_s = (getattr(diarizer, "last_timings", None) or {}).get("merge_small", 0.0)
        if merge_s:
            stages[f"merge_small_{src}"] = merge_s
        stages[f"diarize_{src}"] = max(took_src - merge_s, 0.0)  # excludes merge_small_<src>

    preloaded.clear()
    audio_s = max((len(a) / SAMPLE_RATE for a in audio_by_source.values()), default=None)
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

    progress("Grouping voices", 0.0)
    voice_timings: dict[str, float] = {}
    ranges = tags_mod.tagged_ranges(read_tags(folder), segments)  # the user's names as rules; [] without tags
    final_segments = relabel(segments, words, turns_by_source, diarized, embed, voice_threshold,
                             voice_timings, lambda f: progress("Grouping voices", f), ranges or None, voice)
    final_segments = join_fragments(final_segments)
    if embed is not None:
        stages.update(voice_timings)
    refined = sorted(s for s in diarized
                     if s in audio_by_source and (embed is not None or turns_by_source.get(s)))
    audio_by_source.clear()
    progress("Writing final transcript", 1.0)
    t0 = time.perf_counter()
    version = new_transcript_version()  # every final transcript gets its own, even with the same words
    count = write_transcript(folder.final_tmp_path, folder.meeting_id, started_at, ended_at, final_segments,
                             transcript_version=version)
    os.replace(folder.final_tmp_path, folder.final_path)

    stages["write"] = time.perf_counter() - t0
    try:  # speakers.json: after the final file, before meta and DONE
        write_names(folder, segments, final_segments)
    except Exception:  # noqa: BLE001 - the transcript is done; names can be given again in the window
        log.exception("Could not write speaker names for %s", folder.meeting_id)

    _remove_audio(folder, keep_audio)
    took = time.perf_counter() - t_start
    final_stats = {"took_s": round(took, 2), "audio_s": round(audio_s, 2) if audio_s is not None else None,
                   "speed": round(took / audio_s, 2) if audio_s else None,
                   "stages": {k: round(v, 2) for k, v in stages.items()},
                   **{k: v for k, v in retx.items() if k != "final_transcriber"}}
    extra = {"final_transcriber": retx["final_transcriber"]} if retx.get("final_transcriber") else {}

    folder.update_meta(status=mt.STATUS_COMPLETE, final_written_at=mt.iso(mt.now_local()),
                       final_segments=count, transcript_version=version, audio_kept=folder.audio_dir.exists(),
                       refined_streams=refined, final_stats=final_stats, **extra)
    folder.mark_done()
    return count
