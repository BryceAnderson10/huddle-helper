"""The Meetings list (L29-L31): one ``MeetingInfo`` per meeting folder, and deleting a meeting or its audio.

No Tk. ``scan`` is cheap on a large folder because a meeting is re-read only when something that
feeds its row changed (``meta.json``, ``DONE``, the final transcript, ``speakers.json`` or
``marks.jsonl``, by modification time); the sizes of ``audio\\`` and of the whole folder are measured
by one worker thread of the ``ScanCache`` and show up in a later scan (``audio_bytes`` is None until then).
The window runs ``scan`` on its own worker thread so even the first pass never blocks Tk.

Deleting (``delete_meeting``, ``delete_audio``) is refused for a meeting that is recording, queued or
being processed (``is_busy``), only touches a real folder directly inside the meetings folder, and goes
to the Recycle Bin (``shell.recycle``) unless asked to be permanent or the drive has no Recycle Bin.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import stat
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import marks as marks_mod, meeting as mt, shell, status as status_mod
from .writer import read_events

log = logging.getLogger(__name__)


# -- one row -----------------------------------------------------------------------------
@dataclass(frozen=True)
class MeetingInfo:
    id: str
    path: Path
    title: str
    started_at: str  # ISO 8601 as in meta.json, "" if unknown
    length_s: float | None
    files: status_mod.Files  # what its files say about its status (``status_of`` lays the app's state over it)
    speakers: int | None  # distinct people in the final transcript; None without one
    marks: int
    has_audio: bool  # an audio\ folder exists
    audio_bytes: int | None  # None until the worker has measured it
    total_bytes: int | None
    error: str
    finished_at: str  # final_written_at, else ended_at; "" if unknown
    done_at: float | None = None  # modification time of DONE (what auto-purge ages a meeting by); None without DONE

    @property
    def done(self) -> bool:
        """DONE exists."""
        return self.files.done


def meeting_length_s(meta: dict) -> float | None:
    """How long the meeting was, from meta.json (the recorder's duration_s, else start to end)."""
    if meta.get("duration_s"):
        return float(meta["duration_s"])
    try:
        start = datetime.fromisoformat(meta["started_at"])
        end = datetime.fromisoformat(meta["ended_at"])
        return max(0.0, (end - start).total_seconds()) or None
    except (KeyError, TypeError, ValueError):
        return None


def parse_time(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def hours_since(text: str | None, now: datetime | None = None) -> float | None:
    """Hours between an ISO time and ``now`` (None when it cannot be read)."""
    when = parse_time(text)
    if when is None:
        return None
    now = now or mt.now_local()
    if when.tzinfo is None:
        when = when.astimezone()
    return (now - when).total_seconds() / 3600.0


def _mtime(path: Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _count_speakers(folder: mt.MeetingFolder) -> int | None:
    """People in the final transcript (labels with the same name in speakers.json count once)."""
    if not folder.final_path.exists():
        return None
    try:
        names = folder.read_speakers()
        people = {names.get(e.get("speaker", ""), e.get("speaker", ""))
                  for e in read_events(folder.final_path) if e.get("type") == "segment"}
    except (OSError, ValueError):
        return None
    return len(people)


def read_info(folder: mt.MeetingFolder) -> MeetingInfo | None:
    """One meeting folder as a row, or None if its meta.json cannot be read."""
    try:
        meta = folder.read_meta()
    except (OSError, ValueError):
        return None
    try:
        marks = len(marks_mod.reduce(read_events(folder.marks_path))) if folder.marks_path.exists() else 0
    except (OSError, ValueError):
        marks = int(meta.get("marks") or 0)
    if not isinstance(meta, dict):
        return None
    return MeetingInfo(
        id=folder.meeting_id, path=folder.path, title=str(meta.get("title") or folder.meeting_id),
        started_at=str(meta.get("started_at") or ""), length_s=meeting_length_s(meta),
        files=status_mod.read(folder, meta), speakers=_count_speakers(folder), marks=marks, has_audio=folder.audio_dir.is_dir(),
        audio_bytes=None, total_bytes=None, error=str(meta.get("error") or ""),
        finished_at=str(meta.get("final_written_at") or meta.get("ended_at") or ""),
        done_at=(_mtime(folder.done_path) or 0) / 1e9 or None)


# -- sizes, measured off the Tk thread ---------------------------------------------------------
def dir_size(path: Path) -> int:
    """Bytes under ``path`` (links are not followed; files that vanish meanwhile count as 0)."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class ScanCache:
    """What ``scan`` remembers between calls: parsed rows by folder, and measured sizes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[tuple, MeetingInfo]] = {}
        self._sizes: dict[str, tuple[tuple, int, int]] = {}  # path -> (key, audio bytes, total bytes)
        self._todo: queue.Queue = queue.Queue()
        self._queued: set[str] = set()
        self._worker: threading.Thread | None = None
        self.reads = 0  # folders actually re-read (for tests)

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._measure_loop, name="library-sizes", daemon=True)
            self._worker.start()

    def _measure_loop(self) -> None:
        while True:
            path, key = self._todo.get()
            try:
                audio = dir_size(path / mt.AUDIO)
                total = dir_size(path)
                with self._lock:
                    self._sizes[str(path)] = (key, audio, total)
            except Exception:  # noqa: BLE001 - a folder going away must not stop the worker
                log.exception("Could not measure %s", path)
            finally:
                with self._lock:
                    self._queued.discard(str(path))
                self._todo.task_done()

    def want_size(self, path: Path, key: tuple) -> tuple[int | None, int | None]:
        """Cached sizes for this folder state; queue a measurement when they are missing or stale
        (the old numbers are still returned meanwhile)."""
        with self._lock:
            cached = self._sizes.get(str(path))
            if cached is None or cached[0] != key:
                if str(path) not in self._queued:
                    self._queued.add(str(path))
                    self._todo.put((path, key))
                    self._ensure_worker()
        return (cached[1], cached[2]) if cached is not None else (None, None)

    def wait_for_sizes(self, timeout: float = 10.0) -> bool:
        """Block until the worker is idle (tests). True if it finished in time."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._todo.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def forget(self, meeting_id_or_path) -> None:
        with self._lock:
            self._rows.pop(str(meeting_id_or_path), None)
            self._sizes.pop(str(meeting_id_or_path), None)


def _row_key(folder: mt.MeetingFolder) -> tuple:
    """Everything that feeds a row, by modification time: any change re-reads the folder. The heartbeat
    feeds its status: a recording meeting is re-read at each beat (``status_of`` ages the beat it read)."""
    return (_mtime(folder.meta_path), _mtime(folder.done_path), _mtime(folder.marks_path),
            _mtime(folder.final_path), _mtime(folder.speakers_path), _mtime(folder.audio_dir),
            _mtime(folder.heartbeat_path))


def scan(meetings_dir: Path, cache: ScanCache | None = None) -> list[MeetingInfo]:
    """Every meeting under ``meetings_dir``, newest first. With a ``cache``, a folder is re-read only
    when its row key changed, and sizes come from the cache's worker thread (None until measured)."""
    cache = cache or ScanCache()
    out: list[MeetingInfo] = []
    seen: set[str] = set()
    for folder in mt.iter_meetings(meetings_dir):
        key = str(folder.path)
        seen.add(key)
        rkey = _row_key(folder)
        with cache._lock:
            hit = cache._rows.get(key)
        if hit is not None and hit[0] == rkey:
            info = hit[1]
        else:
            info = read_info(folder)
            if info is None:
                continue
            cache.reads += 1
            with cache._lock:
                cache._rows[key] = (rkey, info)
        audio, total = cache.want_size(folder.path, rkey)
        out.append(replace(info, audio_bytes=audio, total_bytes=total))
    with cache._lock:
        for gone in set(cache._rows) - seen:
            cache._rows.pop(gone, None)
            cache._sizes.pop(gone, None)
    out.sort(key=lambda i: (i.started_at, i.id), reverse=True)
    return out


@dataclass(frozen=True)
class Totals:
    meetings: int
    total_bytes: int
    audio_bytes: int
    measured: bool  # every meeting has been measured


def totals(infos: list[MeetingInfo]) -> Totals:
    """Sizes of all meetings. A meeting whose audio folder was just deleted keeps its old measured sizes until the
    worker measures it again; its audio is counted as gone at once, so the numbers do not lag behind the list."""
    gone = sum(i.audio_bytes or 0 for i in infos if not i.has_audio)
    return Totals(len(infos), sum(i.total_bytes or 0 for i in infos) - gone,
                  sum(i.audio_bytes or 0 for i in infos if i.has_audio), all(i.total_bytes is not None for i in infos))


# -- what the app is doing, and the status shown for each meeting ---------------------------------
@dataclass(frozen=True)
class LiveState:
    """What ``AppController`` is doing right now (``AppController.live_state()``)."""
    recording: str | None = None  # meeting being recorded (also while it stops)
    finalizing: str | None = None  # meeting whose after-Stop pass is running
    progress: float | None = None  # 0-1 of that pass
    queued: frozenset[str] = frozenset()  # waiting for or running the after-Stop pass
    waiting: frozenset[str] = frozenset()  # retries waiting for the recording to stop
    stopping: bool = False  # the recording meeting is stopping (Stop pressed, live transcript finishing)


RECORDING, PROCESSING, QUEUED, RETRY_WAITING = "recording", "processing", "queued", "retry-waiting"
COMPLETE, RECOVERED, FAILED, UNFINISHED = "complete", "recovered", "failed", "unfinished"

FILTERS = ("All", "Recording", "Processing", "Complete", "Interrupted", "Final pass failed", "Not finished")
_FILTER_OF = {RECORDING: "Recording", PROCESSING: "Processing", QUEUED: "Processing", RETRY_WAITING: "Final pass failed",
              COMPLETE: "Complete", RECOVERED: "Interrupted", FAILED: "Final pass failed", UNFINISHED: "Not finished"}


@dataclass(frozen=True)
class StatusView:
    kind: str
    text: str
    busy: bool  # recording, queued or processing (here or in another process): not deletable


def status_of(info: MeetingInfo, live: LiveState | None = None, now: datetime | None = None) -> StatusView:
    """The Status column: ``status.shown`` as a label. A recorder that died shows as "Not finished"
    (recover, at app start, finishes those meetings)."""
    shown = status_mod.shown(info.files, info.id, live, now)
    s = shown.status
    if s in (status_mod.RECORDING, status_mod.STOPPING):
        return StatusView(RECORDING, "Recording", True)
    if s == status_mod.PROCESSING:
        pct = f" {live.progress:.0%}" if shown.held and live.progress is not None else ""
        return StatusView(PROCESSING, f"Processing{pct}", True)
    if s == status_mod.RETRY_WAITING:
        return StatusView(RETRY_WAITING, "Retry queued until recording stops", True)
    if s == status_mod.QUEUED:
        return StatusView(QUEUED, "Waiting for the final pass", True)
    if s == status_mod.COMPLETE:
        return StatusView(RECOVERED if shown.recovered else COMPLETE,
                          "Interrupted (recovered)" if shown.recovered else "Complete", False)
    if s == status_mod.FAILED:
        return StatusView(FAILED, "Final pass failed", False)
    if s == status_mod.INTERRUPTED:
        return StatusView(UNFINISHED, "Interrupted", False)
    return StatusView(UNFINISHED, "Not finished", False)


def filter_name(status: StatusView) -> str:
    return _FILTER_OF.get(status.kind, "All")


def matches(info: MeetingInfo, status: StatusView, *, status_filter: str = "All", search: str = "") -> bool:
    if status_filter != "All" and filter_name(status) != status_filter:
        return False
    needle = search.strip().lower()
    return not needle or needle in info.title.lower() or needle in info.id.lower()


# -- deleting ---------------------------------------------------------------------------------
class DeleteRefused(Exception):
    """The delete was not attempted (busy, or not a meeting folder inside the meetings folder)."""


class MeetingBusy(DeleteRefused):
    pass


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def meeting_folder(meetings_dir: Path, meeting_id: str) -> mt.MeetingFolder:
    """The folder of a meeting, after checking that it is a real meeting folder directly inside the
    meetings folder (not a link, not ``..``, not a hidden staging folder). Raises ``DeleteRefused``."""
    if (not meeting_id or meeting_id.startswith(".") or Path(meeting_id).name != meeting_id
            or meeting_id in (".", "..")):
        raise DeleteRefused(f"{meeting_id!r} is not a meeting id")
    path = Path(meetings_dir) / meeting_id
    if _is_link(path) or not path.is_dir():
        raise DeleteRefused(f"{path} is not a meeting folder")
    if path.resolve().parent != Path(meetings_dir).resolve():
        raise DeleteRefused(f"{path} is not directly inside the meetings folder")
    if not (path / mt.META).is_file():
        raise DeleteRefused(f"{path} has no meta.json")
    return mt.MeetingFolder(path)


def _clear_readonly(func, path, exc) -> None:
    exc = exc[1] if isinstance(exc, tuple) else exc
    if isinstance(exc, FileNotFoundError):
        return
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_permanently(path: Path) -> None:
    """Delete a folder for good. Raises ``OSError`` if anything is left."""
    shutil.rmtree(path, onexc=_clear_readonly)
    if path.exists():
        raise OSError(f"Could not delete {path}")


def _remove(path: Path, permanent: bool, recycler, has_bin) -> str:
    if permanent or not has_bin(path):
        remove_permanently(path)
        return "deleted"
    recycler([path])
    return "recycled"


def delete_meeting(meetings_dir: Path, meeting_id: str, *, is_busy: Callable[[str], bool],
                   permanent: bool = False, recycler=shell.recycle, has_bin=shell.has_recycle_bin) -> str:
    """Delete a whole meeting folder: to the Recycle Bin, or for good when ``permanent`` or the drive has
    no Recycle Bin. Returns "recycled" or "deleted". Raises ``MeetingBusy`` while the meeting is recording,
    queued or being processed, ``DeleteRefused`` for anything that is not a meeting folder, ``OSError``
    if the delete fails."""
    if is_busy(meeting_id):
        raise MeetingBusy(f"{meeting_id} is recording or being processed")
    folder = meeting_folder(meetings_dir, meeting_id)
    return _remove(folder.path, permanent, recycler, has_bin)


def delete_audio(meetings_dir: Path, meeting_id: str, *, is_busy: Callable[[str], bool],
                 permanent: bool = False, recycler=shell.recycle, has_bin=shell.has_recycle_bin) -> str:
    """Delete only the kept audio (``audio\\``) of a meeting and record it in meta.json:
    ``audio_kept: false`` and ``audio_deleted_at``. Returns "recycled", "deleted", or "none" when the
    meeting has no audio folder. Same refusals as ``delete_meeting``."""
    if is_busy(meeting_id):
        raise MeetingBusy(f"{meeting_id} is recording or being processed")
    folder = meeting_folder(meetings_dir, meeting_id)
    audio = folder.audio_dir
    if _is_link(audio):
        raise DeleteRefused(f"{audio} is a link")
    if not audio.is_dir():
        return "none"
    how = _remove(audio, permanent, recycler, has_bin)
    folder.update_meta(audio_kept=False, audio_deleted_at=mt.iso(mt.now_local()))
    return how


@dataclass
class DeleteResult:
    """What ``AppController.delete_meetings`` did, by meeting id."""
    done: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # busy or not a meeting folder
    skipped: list[str] = field(default_factory=list)  # audio only: no audio to delete
    failed: list[tuple[str, str]] = field(default_factory=list)
    how: str = ""  # "recycled" or "deleted" (permanently)

    @property
    def ok(self) -> bool:
        return not (self.refused or self.failed)


def finished_within(info: MeetingInfo, hours: float = 24.0, now: datetime | None = None) -> bool:
    """Finished less than ``hours`` ago (a recording or processing meeting has not finished at all)."""
    age = hours_since(info.finished_at, now)
    return info.done and age is not None and age < hours
