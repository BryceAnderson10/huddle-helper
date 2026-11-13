"""The final pass, as the app and the CLI run it (#61).

``FinalPass`` is built from Settings and has one ``run(folder, on_progress)``. It owns:

- the model wiring: the diarizer, the voice fingerprints and, when the second transcription is on, its
  transcriber (each factory can be replaced, so tests pass fakes);
- the Keep audio rule: the meeting's own Keep audio choice, the Keep audio setting only for a meeting
  without one (``finalize.keeps_audio``);
- the streams to separate into speakers (``SharedMic.separated_streams``);
- progress: ``PassProgress`` values with the stage, the fraction of the whole pass done (stage weights
  applied, never going back) and the time left;
- re-running: an earlier final transcript is replaced and DONE cleared through the meeting folder.

The steps themselves are ``finalize.finalize_meeting``.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, meeting as mt
from .finalize import Stage, Step, finalize_meeting, separated_streams
from .library import meeting_length_s
from .shared_mic import SharedMic

log = logging.getLogger(__name__)

# Shares of the pass (0-1) by stage. Diarization is DIARIZE_SHARE of the speaker work, voice fingerprints
# the rest (final_stats of the test meetings: diarize 87-91%, embed_pieces 9-13%). With the second
# transcription on it comes first and takes SECOND_TRANSCRIPTION_SHARE of the whole pass, a first guess
# (TODO: tune from final_stats.stages.retranscribe of real meetings).
DIARIZE_SHARE = 0.87
SECOND_TRANSCRIPTION_SHARE = 0.5
OWN_RATE_FROM = 0.10  # blend in the pass's own rate once this fraction of the pass is done
HISTORY = 5  # completed meetings whose final_stats.speed are used


@dataclass(frozen=True)
class PassProgress:
    """Where one meeting's final pass is."""
    meeting_id: str
    stage: Stage
    frac: float = 0.0  # 0-1 within the stage
    overall: float = 0.0  # 0-1 over the whole pass, the stage weights applied; never goes back
    elapsed_s: float = 0.0  # since this meeting's pass started
    eta_s: float | None = None  # None until there is history to estimate from: show the stage only

    @property
    def label(self) -> str:
        """The stage as the user sees it: "Identifying speakers (system)", "Grouping voices", ..."""
        return self.stage.label


# -- progress maths (pure) ------------------------------------------------------------------------
def diarize_weights(meta: dict, length_s: float | None) -> dict[str, float]:
    """Seconds of audio the final pass diarizes per stream: the system stream whole, the mic only
    inside its shared mic stretches (an open stretch ends at ``length_s``)."""
    return {"system": float(length_s or 0), "mic": SharedMic.from_meta(meta).mic_seconds(length_s)}


@dataclass(frozen=True)
class ProgressPlan:
    """The stages one meeting's final pass will go through, and what share of the pass each takes."""
    streams: tuple[str, ...]  # separated into speakers, in the order the pass does them
    weights: dict[str, float] | None = None  # seconds of audio each stream diarizes; equal when None
    second_transcription: bool = False

    @classmethod
    def for_meeting(cls, meta: dict, second_transcription: bool) -> "ProgressPlan":
        length_s = meeting_length_s(meta)
        return cls(tuple(separated_streams(meta)), diarize_weights(meta, length_s) if length_s else None,
                   second_transcription)

    @property
    def first_stage(self) -> Stage:
        if self.second_transcription:
            return Stage(Step.SECOND_TRANSCRIPTION)
        return Stage(Step.SEPARATE, self.streams[0]) if self.streams else Stage(Step.GROUP)

    def overall(self, stage: Stage, frac: float) -> float:
        """Fraction (0-1) of the whole pass done at ``frac`` of ``stage``. The write is the end."""
        frac = min(1.0, max(0.0, frac))
        retx = SECOND_TRANSCRIPTION_SHARE if self.second_transcription else 0.0
        if stage.step is Step.SECOND_TRANSCRIPTION:
            return retx * frac
        speakers = 1.0 - retx
        if stage.step is Step.SEPARATE:
            w = [max(float((self.weights or {}).get(s, 1.0)), 0.0) for s in self.streams]
            if not any(w):
                w = [1.0] * len(w)
            idx = self.streams.index(stage.stream) if stage.stream in self.streams else 0
            done = (sum(w[:idx]) + (w[idx] if w else 0.0) * frac) / (sum(w) or 1.0)
            return retx + speakers * DIARIZE_SHARE * done
        if stage.step is Step.GROUP:
            base = retx + (speakers * DIARIZE_SHARE if self.streams else 0.0)
            return base + (1.0 - base) * frac
        return 1.0


def estimate_remaining_s(overall: float, elapsed_s: float, length_s: float | None,
                         speeds: list[float]) -> float | None:
    """Seconds left in the final pass, or None when there is nothing to base it on.

    ``speeds`` are ``final_stats.speed`` (pass time / audio time) of the last completed meetings.
    expected total = median(speeds) x this meeting's length; once the pass is past OWN_RATE_FROM it
    is blended with the pass's own rate (elapsed / overall), weighted by how far along it is;
    remaining = (1 - overall) x expected total. With no history, or no length, there is no estimate
    (the window shows the stage and percent only).
    """
    if not speeds or not length_s or length_s <= 0:
        return None
    overall = min(1.0, max(0.0, overall))
    expected = statistics.median(speeds) * length_s
    if overall > OWN_RATE_FROM and elapsed_s > 0:
        expected = (1.0 - overall) * expected + overall * (elapsed_s / overall)
    return max(0.0, (1.0 - overall) * expected)


def recent_final_speeds(meetings_dir: Path, exclude: str | None = None, limit: int = HISTORY) -> list[float]:
    """``final_stats.speed`` of the last ``limit`` completed meetings (by when their final file was written)."""
    found: list[tuple[str, float]] = []
    for f in mt.iter_meetings(meetings_dir):
        if f.meeting_id == exclude:
            continue
        try:
            meta = f.read_meta()
        except (OSError, ValueError):
            continue
        speed = (meta.get("final_stats") or {}).get("speed")
        if (meta.get("status") in (mt.STATUS_COMPLETE, mt.STATUS_INTERRUPTED)
                and isinstance(speed, (int, float)) and speed > 0):
            found.append((str(meta.get("final_written_at") or meta.get("ended_at") or f.meeting_id), float(speed)))
    found.sort()
    return [speed for _, speed in found[-limit:]]


# -- the pass -------------------------------------------------------------------------------------
_FROM_SETTINGS = object()  # a factory argument left out: build the real model from Settings


class FinalPass:
    def __init__(self, settings: config.Settings, *, busy: Callable[[], bool] = lambda: False,
                 diarizer_factory=_FROM_SETTINGS, embedder_factory=_FROM_SETTINGS,
                 transcriber_factory=_FROM_SETTINGS, clock: Callable[[], float] = time.monotonic):
        """``busy`` says whether a meeting is recording; it is read when each model loads, so a meeting
        started meanwhile gets 1 thread. The factories replace the real models (``None``: no voice
        fingerprints, or no second transcription); left out they are built from ``settings``."""
        self.settings = settings
        self._busy = busy
        self._clock = clock
        self._diarizer_factory = self._diarizer if diarizer_factory is _FROM_SETTINGS else diarizer_factory
        self._embedder_factory = self._embedder if embedder_factory is _FROM_SETTINGS else embedder_factory
        if transcriber_factory is _FROM_SETTINGS:
            from .transcribe import final_transcriber_factory

            transcriber_factory = final_transcriber_factory(settings, self._threads)
        self._transcriber_factory = transcriber_factory

    def _threads(self) -> int:
        from .diarize import final_threads

        return final_threads(self._busy())

    def _diarizer(self):
        from .diarize import SherpaOfflineDiarizer

        return SherpaOfflineDiarizer(threshold=self.settings.voice_profile().split_cluster, merge_small=False,
                                     num_threads=self._threads())

    def _embedder(self):
        from .diarize import SherpaEmbedder

        return SherpaEmbedder(num_threads=self._threads())

    def run(self, folder: mt.MeetingFolder,
            on_progress: Callable[[PassProgress], None] | None = None) -> int:
        """Run the final pass on ``folder``, replacing any earlier final transcript, and mark it done.
        Returns the number of final lines. ``on_progress`` gets a ``PassProgress`` at the start and at
        every step; its ``overall`` never goes back. Raises if the pass fails."""
        report = on_progress or (lambda p: None)
        t_start = self._clock()
        mid = folder.meeting_id
        second = self._transcriber_factory is not None
        plan = ProgressPlan((), None, second)
        length_s: float | None = None
        speeds: list[float] = []
        try:
            meta = folder.read_meta()
            plan = ProgressPlan.for_meeting(meta, second)
            length_s = meeting_length_s(meta)
            speeds = recent_final_speeds(folder.path.parent, exclude=mid)
        except Exception:  # noqa: BLE001 - the estimate is optional; the pass itself reads meta again
            log.exception("Could not prepare the progress estimate for %s", mid)

        def progress(stage: Stage, frac: float) -> None:
            overall = plan.overall(stage, frac)
            elapsed = self._clock() - t_start
            report(PassProgress(mid, stage, min(1.0, max(0.0, frac)), overall, elapsed,
                                estimate_remaining_s(overall, elapsed, length_s, speeds)))

        progress(plan.first_stage, 0.0)
        return finalize_meeting(folder, self._diarizer_factory, keep_audio=self.settings.keep_audio,
                                progress=progress, embedder_factory=self._embedder_factory,
                                voice=self.settings.voice_profile(), transcriber_factory=self._transcriber_factory)
