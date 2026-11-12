"""Click to hear (WP7, L15-L18): clips of the recorded audio, and the player that speaks them.

Pure parts (no Tk, no audio device, easy to test):

* ``clip_range`` / ``clip_from_files`` / ``clip_from_ring``   the padded clip of one stream: the line's
  own span plus 300 ms each side so the first and last words are not clipped (the end runs on to a quiet
  point when speech still continues there, and the start skips the previous speaker's last sounds:
  ``snap_end`` / ``snap_start``); read from the kept
  ``audio\\*.flac`` (inside a ``with`` block, so no file handle outlives the read: the folder can be
  deleted at once) or from the recorder's in-memory ring while recording. Quiet clips are raised with
  the speech-level rule of ``leveling.level_clip``, for playback only.
* ``mix`` / ``bookmark_range`` / ``Clip`` / ``line_clip`` / ``bookmark_clip`` / ``pick_voice_line``
  what to play for a line, a bookmark ("what you heard": both streams, 10 s before to 5 s after) or
  a speaker ("Hear this voice").
* ``PlaybackWindows``   the drop rule (plan 4.7, decision 2): a clip played while recording is logged as
  a window on the meeting clock; an utterance that overlaps a window and repeats the clip's words is
  dropped before it can become a line or found a live speaker.
* ``headphone_check``   the once-per-meeting warning and the confirmation when echo dedupe has already
  dropped mic lines.

Impure part:

* ``Player``   one clip at a time on a worker thread. A ``PyAudio`` instance is opened per clip and
  terminated when the clip ends (PortAudio's init is reference-counted, so a playback instance left
  open would stop ``capture._rebind`` from refreshing the device list: plan decision 7). The clip is
  resampled with soxr to the output device's default rate, because a 16 kHz output stream fails with
  "Invalid sample rate" on a 48 kHz device, and mono is duplicated to the device's channels.
  ``gain`` (default 1.0, which the app uses) scales the output; scripts that must be quiet pass less.
  Unit tests inject ``output_factory`` and never open a device.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from . import echo
from .audio import SAMPLE_RATE
from .leveling import level_clip

log = logging.getLogger(__name__)

PAD_MS = 300  # added before and after a line
BOOKMARK_BEFORE_MS, BOOKMARK_AFTER_MS = 10_000, 5_000
VOICE_MIN_S, VOICE_MAX_S = 3.0, 8.0  # "Hear this voice" prefers a line this long
HIGH_CONFIDENCE = 0.75
MAX_CLIP_S = 30.0  # nothing longer is played (a runaway range, not a real line)
WINDOW_BEFORE_MS = 250  # a window opens this long before the play call...
WINDOW_LEAD_MS = 1_000  # ...the device takes up to this long to start (a PyAudio instance is opened per clip)...
WINDOW_TAIL_MS = 1_500  # ...and the loopback and the transcriber see the end this much late
RUN_JOIN_MS = 12_000  # a joined window spans the clips started in the last 12 s (max_utterance_s)
RING_SECONDS = 600  # the in-memory recent audio, per stream

HEADPHONES = "Use headphones: without them, people on the call will hear this clip."
HEADPHONES_CONFIRM = ("Use headphones: without them, people on the call will hear this clip. "
                      "Your microphone has already picked up the call's sound in this meeting, "
                      "which usually means you are not wearing headphones. Play it anyway?")

# -- reading clips ------------------------------------------------------------------------------


def clip_range(start_ms: int, end_ms: int, pad_ms: int = PAD_MS) -> tuple[int, int]:
    """The span to read for a line: ``start - pad .. end + pad``, not before the start of the meeting."""
    return max(0, int(start_ms) - pad_ms), max(0, int(end_ms) + pad_ms)


def read_file_span(path: Path, start_ms: int, end_ms: int) -> np.ndarray | None:
    """``start_ms .. end_ms`` of an audio file as mono float32 at 16 kHz, clamped to the file.

    Uses frame offsets (seek, read) so a clip from late in a long recording does not load the
    recording, and a ``with`` block so the handle is closed before this returns. None when the file is
    missing or unreadable, or the span is empty after clamping.
    """
    import soundfile as sf

    if not Path(path).is_file():
        return None  # gone (Keep audio off, a delete) is expected, not an error worth a traceback
    try:
        with sf.SoundFile(str(path)) as f:
            rate = f.samplerate
            a = min(max(0, int(start_ms) * rate // 1000), f.frames)
            b = min(max(0, int(end_ms) * rate // 1000), f.frames)
            if b <= a:
                return None
            f.seek(a)
            data = f.read(b - a, dtype="float32", always_2d=True)
    except (OSError, RuntimeError, sf.LibsndfileError):
        log.exception("Could not read audio from %s", path)
        return None
    data = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if rate != SAMPLE_RATE:
        import soxr

        data = soxr.resample(data, rate, SAMPLE_RATE).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32)


EXTRA_MS = 600  # how far beyond the padding a clip may reach to find a quiet point, at either end
FRAME_MS = 20
QUIET = 0.12  # a 20 ms step is quiet below this fraction of the line's loud steps (about -18 dB)
FADE_MS = 15
GUARD_MS = 40  # a start moved forward stays this far before the line


def _rms(x: np.ndarray, i: int, n: int) -> float:
    f = x[max(0, i):i + n]
    return float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) else 0.0


def _frame_rms(x: np.ndarray, n: int) -> np.ndarray:
    k = len(x) // n
    return np.sqrt(np.mean(x[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1)) if k else np.zeros(0)


def _quiet_level(x: np.ndarray, seg: np.ndarray, rate: int = SAMPLE_RATE) -> float:
    """The rms below which a 20 ms step counts as quiet: ``QUIET`` of the line's loud steps (their 90th
    percentile; the mean would be dragged down by pauses inside the line), but not below 2.5 times the
    stream's noise floor here (Test #3's mic hisses at a fifth of its speech level) and never above 30% of the
    loud steps."""
    n = rate * FRAME_MS // 1000
    loud_frames, all_frames = _frame_rms(seg, n), _frame_rms(x, n)
    loud = float(np.percentile(loud_frames, 90)) if len(loud_frames) else 0.0
    floor = float(np.percentile(all_frames, 10)) if len(all_frames) else 0.0
    return min(max(QUIET * loud, 2.5 * floor), 0.3 * loud)


def snap_end(x: np.ndarray, body: slice, end: int, rate: int = SAMPLE_RATE) -> int:
    """Where a line's clip should end, as a sample index ``>= end`` (``end`` is the padded end).

    Whisper's last-word time is tight: in Tests #2 and #3 about a quarter of the lines still had speech
    (the end of the last word, a breath) 300-650 ms after it, so a fixed 300 ms pad cut them. If the audio
    at ``end`` is not quiet compared with the line itself (``x[body]``), run on in 20 ms steps, at most
    ``EXTRA_MS`` more, to the first quiet step. With no quiet step in reach (the next speaker follows at
    once) the clip is left at ``end``: never sweep into the next person's words."""
    n = rate * FRAME_MS // 1000
    seg = np.asarray(x[body], dtype=np.float32)
    end = min(end, len(x))
    if len(seg) < n or end >= len(x):
        return end
    quiet = _quiet_level(x, seg, rate)
    if _rms(x, end - n, n) < quiet:
        return end  # already ends in quiet: the usual case
    for i in range(end, min(len(x), end + rate * EXTRA_MS // 1000) - n + 1, n):
        if _rms(x, i, n) < quiet:
            return i + n
    return end


def snap_start(x: np.ndarray, body: slice, start: int, rate: int = SAMPLE_RATE) -> int:
    """Where a line's clip should start, as a sample index (``start`` is the padded start).

    If the audio at ``start`` is quiet, that is it. If not, either the previous speaker's last sounds are
    still running (then a quiet gap lies between them and the line: start at its first quiet 20 ms step,
    ending 40 ms or more before the line) or the line's first word began earlier than Whisper said (Test
    #3's quiet mic line began 370 ms early: no quiet step before the line, so go back, at most ``EXTRA_MS``,
    to the nearest quiet one). Neither in reach: ``start`` stays."""
    n = rate * FRAME_MS // 1000
    seg = np.asarray(x[body], dtype=np.float32)
    start = max(0, start)
    if len(seg) < n:
        return start
    quiet = _quiet_level(x, seg, rate)
    if _rms(x, start, n) < quiet:
        return start
    first = body.start or 0
    for i in range(start, first - rate * GUARD_MS // 1000 - n + 1, n):  # forward: skip the previous speaker
        if _rms(x, i, n) < quiet:
            return i
    for i in range(start - n, max(0, start - rate * EXTRA_MS // 1000) - 1, -n):  # backward: an early onset
        if _rms(x, i, n) < quiet:
            return i
    return start


def cut_line(x: np.ndarray, x_ms: int, start_ms: int, end_ms: int, pad_ms: int = PAD_MS) -> tuple[np.ndarray, int]:
    """``x`` is audio of one stream beginning at meeting time ``x_ms`` that reaches ``EXTRA_MS`` before the
    padded start and after the padded end of the line. Returns the clip (a view) and its start time in
    ms: the padding of ``clip_range``, moved to quiet points by ``snap_start`` / ``snap_end``."""
    per_ms = SAMPLE_RATE // 1000
    body = slice(max(0, (start_ms - x_ms) * per_ms), max(0, (end_ms - x_ms) * per_ms))
    first = snap_start(x, body, max(0, (start_ms - pad_ms - x_ms) * per_ms))
    last = snap_end(x, body, min(len(x), (end_ms + pad_ms - x_ms) * per_ms))
    return x[first:max(first, last)], x_ms + first // per_ms


def _finish_line(x: np.ndarray, x_ms: int, start_ms: int, end_ms: int, pad_ms: int) -> np.ndarray:
    """``cut_line`` plus a few ms of fade-out, so a cut mid-sound does not click."""
    y = np.array(cut_line(x, x_ms, start_ms, end_ms, pad_ms)[0], dtype=np.float32)
    fade = min(len(y), FADE_MS * (SAMPLE_RATE // 1000))
    if fade:
        y[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return y


def clip_from_files(folder, source: str, start_ms: int, end_ms: int, pad_ms: int = PAD_MS) -> np.ndarray | None:
    """The clip of a line from the meeting's kept audio: padded (the ends moved to quiet points, see
    ``snap_start`` / ``snap_end``), quiet clips raised. None if there is no such audio (file missing, or
    the line lies outside it)."""
    a, b = clip_range(start_ms, end_ms, pad_ms)
    x_ms = max(0, a - EXTRA_MS)
    data = read_file_span(folder.audio_file(source), x_ms, b + EXTRA_MS)
    return None if data is None else level_clip(_finish_line(data, x_ms, start_ms, end_ms, pad_ms))


def clip_from_ring(ring, start_ms: int, end_ms: int, pad_ms: int = PAD_MS) -> np.ndarray | None:
    """The same from the recorder's ``RecentAudio``. None when the line itself has left the window; the
    padding alone is allowed to fall outside it (the clip then starts at the oldest audio held)."""
    if ring is None or start_ms < ring.oldest_ms:
        return None
    a, b = clip_range(start_ms, end_ms, pad_ms)
    x_ms = max(a - EXTRA_MS, ring.oldest_ms)
    data = ring.read(x_ms, b + EXTRA_MS)
    return None if data is None else level_clip(_finish_line(data, x_ms, start_ms, end_ms, pad_ms))


def mixed_from_files(folder, clip: "Clip") -> np.ndarray | None:
    """A multi-stream clip (a bookmark) from the kept audio: the raw spans summed, then levelled once."""
    got = mix([read_file_span(folder.audio_file(src), clip.start_ms, clip.end_ms) for src in clip.sources])
    return None if got is None else level_clip(got)


def mixed_from_rings(rings: dict, clip: "Clip") -> np.ndarray | None:
    """The same from the recorder's rings. Unlike a line, a bookmark clip may start before the oldest
    audio held (it is clamped to it); a stream with nothing in range simply adds nothing."""
    parts = []
    for src in clip.sources:
        ring = rings.get(src)
        if ring is not None:
            parts.append(ring.read(max(clip.start_ms, ring.oldest_ms), clip.end_ms))
    got = mix(parts)
    return None if got is None else level_clip(got)


def mix(clips: Sequence[np.ndarray | None]) -> np.ndarray | None:
    """Sum clips of the same span (the streams a person heard together); the shorter is zero-padded.
    A single clip is returned as it is; peaks above 1 are scaled down, not clipped."""
    got = [c for c in clips if c is not None and len(c)]
    if not got:
        return None
    if len(got) == 1:
        return got[0]
    out = np.zeros(max(len(c) for c in got), dtype=np.float32)
    for c in got:
        out[:len(c)] += c
    peak = float(np.abs(out).max())
    return out / peak if peak > 1.0 else out


# -- what to play ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    """A request to play: ``key`` is the identity the play/stop control toggles on."""
    key: str
    sources: tuple[str, ...]  # one stream for a line, both for a bookmark
    start_ms: int  # unpadded span
    end_ms: int
    text: str  # what is said in it, for the drop rule
    pad_ms: int = PAD_MS  # a line of "Play from here" carries less padding (playsequence.RUN_PAD_MS)

    @property
    def duration_s(self) -> float:
        return max(0, self.end_ms - self.start_ms) / 1000


def line_key(source: str, start_ms: int) -> str:
    return f"line:{source}:{start_ms}"


def line_clip(row) -> Clip:
    return Clip(line_key(row.source, row.start_ms), (row.source,), row.start_ms, row.end_ms, row.text)


def bookmark_range(at_ms: int, before_ms: int = BOOKMARK_BEFORE_MS,
                   after_ms: int = BOOKMARK_AFTER_MS) -> tuple[int, int]:
    """The span of the meeting clock around a bookmark, by its ``at_ms`` only (never a line's ``seq``):
    10 s before to 5 s after it unless told otherwise."""
    return max(0, at_ms - before_ms), at_ms + after_ms


def bookmark_clip(rows: Iterable, mark) -> Clip:
    """A bookmark plays what was heard around it: both streams mixed, ``bookmark_range`` of the
    meeting clock. ``text`` is every line in that span, for the drop rule."""
    start, end = bookmark_range(mark.at_ms)
    said = [r for r in rows if hasattr(r, "text") and hasattr(r, "source") and r.end_ms > start and r.start_ms < end]
    said.sort(key=lambda r: (r.start_ms, r.source))
    return Clip(f"mark:{mark.id}", ("mic", "system"), start, end, " ".join(r.text.strip() for r in said))


def pick_voice_line(rows: Iterable, person_key: str, can_play: Callable[[object], bool] | None = None):
    """The line "Hear this voice" plays for ``person_key``: of that person's high-confidence lines (all
    their lines if none is), the longest one of 3-8 s; failing that the longest of any length.
    ``can_play`` leaves out lines whose audio is not available. None if there is no candidate."""
    mine = [r for r in rows if hasattr(r, "person_key") and r.person_key == person_key and r.text.strip()
            and (can_play is None or can_play(r))]
    if not mine:
        return None
    pool = [r for r in mine if r.confidence >= HIGH_CONFIDENCE] or mine

    def length(r) -> int:
        return r.end_ms - r.start_ms

    ideal = [r for r in pool if VOICE_MIN_S * 1000 <= length(r) <= VOICE_MAX_S * 1000]
    return max(ideal or pool, key=lambda r: (length(r), r.confidence, -r.start_ms))


# -- the drop rule --------------------------------------------------------------------------------


@dataclass
class _Window:
    start_ms: int
    end_ms: int
    words: list[str]


class PlaybackWindows:
    """Clips played while recording, on the meeting clock, and the utterances dropped because of them.

    ``match`` is called from the transcribe loop for every transcribed utterance, before the speaker
    is assigned. It drops (returns True) only when the utterance overlaps a window in time *and* repeats
    that window's words (``echo.overlap_fraction``, the echo dedupe's 0.6 threshold; two words or fewer
    must all match). Overlap without a match keeps the utterance, so a remote person talking over a clip
    is not lost. Thread-safe: ``add`` from the Tk thread, ``match`` from the transcriber thread."""

    THRESHOLD = 0.6

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: list[_Window] = []
        self._run: list[tuple[int, str]] = []  # (start, text) of the latest clips of the run in progress
        self.count = 0  # clips played
        self.dropped = 0  # utterances dropped

    def add(self, now_ms: int, duration_s: float, text: str) -> None:
        """A clip of ``duration_s`` starts being played at meeting time ``now_ms``."""
        win = _Window(max(0, now_ms - WINDOW_BEFORE_MS),
                      int(now_ms + WINDOW_LEAD_MS + duration_s * 1000 + WINDOW_TAIL_MS), echo.normalize_words(text))
        with self._lock:
            self._windows.append(win)
            self.count += 1

    def add_run_clip(self, now_ms: int, duration_s: float, text: str) -> None:
        """One clip of a continuous run (Play from here). Besides its own window, a *joined* window covers
        the clips started in the last ``RUN_JOIN_MS`` of the run together (the longest utterance the
        transcriber cuts): clips with under half a second between them come back through the loopback as one
        utterance holding the words of several, which matched against one clip's words alone falls under
        the 0.6 threshold and would become a transcript line. A joined window is not counted as a clip."""
        self.add(now_ms, duration_s, text)
        end = int(now_ms + WINDOW_LEAD_MS + duration_s * 1000 + WINDOW_TAIL_MS)
        with self._lock:
            self._run.append((now_ms, text))
            self._run = [r for r in self._run if r[0] >= now_ms - RUN_JOIN_MS]
            if len(self._run) > 1:
                words = echo.normalize_words(" ".join(r[1] for r in self._run))
                self._windows.append(_Window(max(0, self._run[0][0] - WINDOW_BEFORE_MS), end, words))

    def end_run(self) -> None:
        with self._lock:
            self._run = []

    def match(self, start_ms: int, end_ms: int, text: str) -> bool:
        words = echo.normalize_words(text)
        if not words:
            return False
        threshold = 1.0 if len(words) <= 2 else self.THRESHOLD
        with self._lock:
            near = [w for w in self._windows if start_ms < w.end_ms and end_ms > w.start_ms]
            hit = any(echo.overlap_fraction(words, w.words) >= threshold for w in near)
            if hit:
                self.dropped += 1
            return hit

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.count, self.dropped


# -- headphones -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadphoneCheck:
    action: str  # "play" | "warn" (play, and tell the user once) | "confirm" (ask first)
    message: str = ""


def headphone_check(recording: bool, warned: bool, confirmed: bool, echo_dropped: int) -> HeadphoneCheck:
    """What to do before playing a clip. Only while recording is anything said; the plain warning is
    once per meeting; if echo dedupe has already dropped mic lines (a sign of speakers, no headphones)
    the user must confirm first, once per meeting."""
    if not recording:
        return HeadphoneCheck("play")
    if echo_dropped > 0 and not confirmed:
        return HeadphoneCheck("confirm", HEADPHONES_CONFIRM)
    if not warned:
        return HeadphoneCheck("warn", HEADPHONES)
    return HeadphoneCheck("play")


# -- the player ---------------------------------------------------------------------------------------


class PortAudioOutput:
    """One output stream, with its own ``PyAudio`` instance. ``device_name`` is the user's "Play clips on"
    choice (``outputs.py``); None, or a name that is not found any more, plays on the Windows default
    output (``fell_back`` says the latter). ``close`` terminates the instance, so nothing of PortAudio
    stays initialised between clips."""

    def __init__(self, device_name: str | None = None) -> None:
        import pyaudiowpatch as pyaudio

        from . import outputs

        self._pa = pyaudio.PyAudio()
        self._stream = None
        self.fell_back = False
        try:
            wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            infos = [self._pa.get_device_info_by_index(i) for i in range(self._pa.get_device_count())]
            pick = outputs.pick_output(outputs.read_outputs(infos, wasapi["index"]), device_name,
                                       wasapi["defaultOutputDevice"])
            if pick.device is None:
                raise OSError("No output device")
            dev = self._pa.get_device_info_by_index(pick.device.index)
            self.fell_back = pick.fell_back
            self.rate = int(dev["defaultSampleRate"])
            self.channels = max(1, int(dev["maxOutputChannels"]))
            self.device_name = dev["name"]
            self._stream = self._pa.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
                                         output=True, output_device_index=int(dev["index"]))
        except Exception:
            self.close()
            raise

    def write(self, pcm: bytes) -> None:
        self._stream.write(pcm)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except OSError:
            log.exception("Could not close the playback stream")
        finally:
            pa, self._pa = self._pa, None
            if pa is not None:
                pa.terminate()


def prepare_pcm(samples: np.ndarray, rate: int, channels: int, gain: float = 1.0) -> np.ndarray:
    """16 kHz mono float -> int16 frames ``(n, channels)`` at the device's rate: resampled with soxr,
    scaled by ``gain`` (0-1), mono duplicated to every channel."""
    gain = min(1.0, max(0.0, float(gain)))
    x = np.asarray(samples, dtype=np.float32)
    if rate != SAMPLE_RATE and len(x):
        import soxr

        x = soxr.resample(x, SAMPLE_RATE, rate).astype(np.float32)
    x = np.clip(x * gain, -1.0, 1.0)
    pcm = (x * 32767.0).astype(np.int16)
    return np.repeat(pcm[:, None], max(1, channels), axis=1)


class Player:
    """One clip at a time on a worker thread. ``play`` stops whatever is playing first.

    ``on_done(reason)`` is called from the worker thread with ``"finished"``, ``"stopped"`` or
    ``"error: ..."``; it must only hand the result on (set a flag, post to the Tk thread).
    ``stop()`` waits for the worker to finish and for PortAudio to be terminated, so it can be called
    right before the capture re-opens its devices; it is safe from any thread and does nothing when idle.
    """

    BLOCK_S = 0.1  # written in blocks this long, so stop is heard within a block

    def __init__(self, gain: float = 1.0, output_factory: Callable[[], object] | None = None,
                 device_name: str | None = None):
        self.gain = min(1.0, max(0.0, float(gain)))
        self.device_name = device_name  # "Play clips on": None = the Windows default output
        self.on_fallback: Callable[[str], None] | None = None  # told when the chosen device was not found
        self._factory = output_factory or self._open_output
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    def _open_output(self):
        wanted = self.device_name
        out = PortAudioOutput(wanted)
        if out.fell_back and self.on_fallback is not None and wanted:
            try:
                self.on_fallback(wanted)
            except Exception:  # noqa: BLE001
                log.exception("Playback fallback callback failed")
        return out

    @property
    def playing(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def play(self, samples: np.ndarray, on_done: Callable[[str], None] | None = None) -> None:
        self.stop()
        with self._lock:
            cancel = threading.Event()
            self._cancel = cancel
            self._thread = threading.Thread(target=self._run, args=(np.array(samples, dtype=np.float32), cancel, on_done),
                                            name="playback", daemon=True)
            self._thread.start()

    def play_run(self, clips: Sequence["Clip"], load: Callable[["Clip"], np.ndarray | None],
                 on_clip: Callable[["Clip", np.ndarray], None] | None = None,
                 on_done: Callable[[str], None] | None = None) -> None:
        """Play ``clips`` one after the other on one output stream (Play from here, ``playsequence``).
        ``load`` and ``on_clip`` run on the worker thread; ``on_done`` as for ``play``, plus
        ``"unavailable"`` when no clip could be read."""
        self.stop()
        with self._lock:
            cancel = threading.Event()
            self._cancel = cancel
            self._thread = threading.Thread(target=self._run_clips, args=(list(clips), load, on_clip, cancel, on_done),
                                            name="playback-run", daemon=True)
            self._thread.start()

    def _run_clips(self, clips, load, on_clip, cancel, on_done) -> None:
        from . import playsequence

        try:
            reason = playsequence.run_sequence(self._factory, clips, load, cancel, self.gain, on_clip).reason
        except Exception as exc:  # noqa: BLE001 - a device problem must not leave the app stuck "playing"
            log.exception("Playback failed")
            reason = f"error: {exc}"
        if on_done is not None:
            try:
                on_done(reason)
            except Exception:  # noqa: BLE001
                log.exception("Playback callback failed")

    def stop(self) -> None:
        with self._lock:
            thread, cancel = self._thread, self._cancel
            cancel.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run(self, samples: np.ndarray, cancel: threading.Event, on_done) -> None:
        reason = "finished"
        out = None
        try:
            out = self._factory()
            frames = prepare_pcm(samples, out.rate, out.channels, self.gain)
            block = max(1, int(out.rate * self.BLOCK_S))
            for i in range(0, len(frames), block):
                if cancel.is_set():
                    reason = "stopped"
                    break
                out.write(frames[i:i + block].tobytes())
        except Exception as exc:  # noqa: BLE001 - a device problem must not leave the app stuck "playing"
            log.exception("Playback failed")
            reason = f"error: {exc}"
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:  # noqa: BLE001
                    log.exception("Could not close the playback device")
        if on_done is not None:
            try:
                on_done(reason)
            except Exception:  # noqa: BLE001
                log.exception("Playback callback failed")


# -- is there audio to play? (the disabled-control reasons, LIVE_VIEW "When audio exists") -------------

DELETED_AFTER_FINAL = "Audio was deleted after the final transcript. Turn on Keep audio to hear lines later."
DELETED_BY_USER = "This meeting's audio was deleted from the Meetings list."
PURGED = "This meeting's audio was removed by the automatic purge."
AFTER_STOP = "Older than the last 10 minutes. Available after Stop."
NOT_READABLE = "Not readable yet. Available when the live transcript is finished."
NO_STREAM = "This meeting has no audio for that stream."
NO_AUDIO = "This meeting has no audio."


@dataclass(frozen=True)
class PlayState:
    enabled: bool
    reason: str = ""  # why not, for the tooltip


@dataclass(frozen=True)
class AudioContext:
    """Where the displayed meeting's audio can be read from right now. Cheap to build every half second
    (two ``exists`` checks) and cheap to ask per line."""
    mode: str  # "ring" (being recorded) | "files" (kept audio on disk) | "none"
    reason: str = ""  # mode "none": why, for every line
    present: frozenset[str] = frozenset()  # mode "files": streams with a file
    oldest_ms: tuple[tuple[str, int], ...] = ()  # mode "ring": earliest time held, per stream

    def state(self, source: str, start_ms: int) -> PlayState:
        if self.mode == "none":
            return PlayState(False, self.reason)
        if self.mode == "files":
            return PlayState(True) if source in self.present else PlayState(False, NO_STREAM)
        oldest = dict(self.oldest_ms)
        if source not in oldest:
            return PlayState(False, NO_STREAM)
        return PlayState(True) if start_ms >= oldest[source] else PlayState(False, AFTER_STOP)

    def clip_state(self, clip: "Clip") -> PlayState:
        """A multi-stream clip (bookmark) plays if any of its streams has audio in its span."""
        if len(clip.sources) == 1:
            return self.state(clip.sources[0], clip.start_ms)
        states = [self.state(src, clip.end_ms if self.mode == "ring" else clip.start_ms) for src in clip.sources]
        return next((s for s in states if s.enabled), states[0])

    @property
    def signature(self) -> tuple:
        """Changes when the enabled/disabled look of the rows can change (the ring's edge is rounded to 10 s)."""
        return (self.mode, self.reason, self.present, tuple((s, ms // 10_000) for s, ms in self.oldest_ms))


def audio_context(*, recording: bool, stopping: bool, done: bool, meta: dict, present: Iterable[str],
                  oldest_ms: dict[str, int] | None = None) -> AudioContext:
    """``recording``/``stopping``: the displayed meeting is the one being recorded (live / finishing);
    ``done``: its ``DONE`` exists; ``present``: streams that have a file in the audio folder; ``oldest_ms``:
    the recorder's ``recent_oldest_ms`` while recording."""
    if recording and oldest_ms is not None:
        return AudioContext("ring", oldest_ms=tuple(sorted(oldest_ms.items())))
    if stopping or recording:
        return AudioContext("none", NOT_READABLE)
    present = frozenset(present)
    if present:
        return AudioContext("files", present=present)
    if meta.get("audio_deleted_at"):
        return AudioContext("none", DELETED_BY_USER)
    if meta.get("audio_purged_at"):
        return AudioContext("none", PURGED)
    return AudioContext("none", DELETED_AFTER_FINAL if done else NO_AUDIO)
