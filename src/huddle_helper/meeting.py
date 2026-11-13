"""Meeting folder layout and metadata (F7).

Layout (see docs/OUTPUT_SPEC.md):

    <meetings>/<meeting_id>/
        meta.json               start time, devices, models, status
        transcript.jsonl        live, append-only
        transcript.final.jsonl  written after Stop (via a temp name + rename)
        speakers.json           optional speaker id -> display name
        marks.jsonl             bookmarks (first bookmark creates it)
        speaker_tags.jsonl      the user's speaker names (first name creates it)
        echo_dropped.jsonl      mic lines dropped as echo (first drop creates it)
        heartbeat.json          the recorder's "still alive" time, refreshed while recording
        DONE                    empty marker, created last
        audio/                  buffered audio, deleted unless Keep audio is on
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .shared_mic import SharedMic

META = "meta.json"
LIVE = "transcript.jsonl"
FINAL = "transcript.final.jsonl"
FINAL_TMP = "transcript.final.jsonl.tmp"
SPEAKERS = "speakers.json"
MARKS = "marks.jsonl"
TAGS = "speaker_tags.jsonl"
ECHO_DROPPED = "echo_dropped.jsonl"
HEARTBEAT = "heartbeat.json"
DONE = "DONE"
AUDIO = "audio"

MUTED = "muted"  # gap reason: the user took their mic out of the recording (not lost audio)

STATUS_RECORDING = "recording"
STATUS_PROCESSING = "processing"
STATUS_COMPLETE = "complete"
STATUS_INTERRUPTED = "interrupted"
STATUS_FAILED = "failed"


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def change_time(previous: str | None = None) -> str:
    """Now, in ISO 8601 with milliseconds, for a "last changed" time (speaker names, bookmarks).

    Strictly later than ``previous`` (the time the thing last changed), so two changes in the same
    millisecond still give a reader two different times."""
    now = now_local()
    try:
        before = datetime.fromisoformat(previous) if previous else None
    except (TypeError, ValueError):
        before = None
    if before is not None and before.tzinfo is not None and now <= before:
        now = (before + timedelta(milliseconds=1)).astimezone(now.tzinfo)
    return now.isoformat(timespec="milliseconds")


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].strip("-") or "meeting"


def make_meeting_id(started: datetime, title: str) -> str:
    return f"{started:%Y-%m-%d_%H%M}_{slugify(title)}"


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_heartbeat(path: Path, at: datetime) -> None:
    """Replace heartbeat.json atomically (temp file + rename). No fsync: after a crash the heartbeat is
    stale whatever it holds, so it is not worth a disk flush every few seconds."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"at": at.isoformat(timespec="milliseconds")}, fh)
    os.replace(tmp, path)


_FOLDER_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def folder_lock(path: Path) -> threading.Lock:
    """One lock per resolved folder path, shared by every MeetingFolder for that folder."""
    key = os.path.normcase(str(Path(path).resolve()))
    with _REGISTRY_LOCK:
        lock = _FOLDER_LOCKS.get(key)
        if lock is None:
            lock = _FOLDER_LOCKS[key] = threading.Lock()
        return lock


def append_jsonl_unlocked(path: Path, event: dict) -> None:
    """Append one compact JSON line and fsync. The caller holds ``folder_lock`` (it needs the lock
    for more than the write, e.g. to read the file first)."""
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def append_jsonl(path: Path, event: dict) -> None:
    """Append one compact JSON line to a file in a meeting folder, under that folder's lock."""
    path = Path(path)
    with folder_lock(path.parent):
        append_jsonl_unlocked(path, event)


class MeetingFolder:
    """A single meeting's folder plus its metadata."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def _lock(self) -> threading.Lock:
        return folder_lock(self.path)

    # -- creation -------------------------------------------------------
    @classmethod
    def create(cls, meetings_dir: Path, title: str, started: datetime, meta: dict) -> "MeetingFolder":
        """Create the folder with meta.json already in place.

        The folder is built under a hidden temporary name and renamed into
        place, so a watcher never sees a meeting folder without meta.json.
        """
        meetings_dir = Path(meetings_dir)
        meetings_dir.mkdir(parents=True, exist_ok=True)
        base_id = make_meeting_id(started, title)
        meeting_id = base_id
        n = 2
        while (meetings_dir / meeting_id).exists():
            meeting_id = f"{base_id}-{n}"
            n += 1

        staging = meetings_dir / f".{meeting_id}.creating"
        staging.mkdir(parents=True, exist_ok=False)
        (staging / AUDIO).mkdir()
        full_meta = {
            "meeting_id": meeting_id,
            "title": title,
            "schema": SCHEMA_VERSION,
            "app_version": __version__,
            "status": STATUS_RECORDING,
            "clean_exit": None,
            "started_at": iso(started),
            "ended_at": None,
            "final_written_at": None,
            "segments": 0,
            "gaps": [],
            **meta,
        }
        write_json_atomic(staging / META, full_meta)
        write_heartbeat(staging / HEARTBEAT, started)  # status is recording: alive from the first moment
        final = meetings_dir / meeting_id
        os.replace(staging, final)
        return cls(final)

    # -- paths ----------------------------------------------------------
    @property
    def meeting_id(self) -> str:
        return self.path.name

    @property
    def meta_path(self) -> Path:
        return self.path / META

    @property
    def live_path(self) -> Path:
        return self.path / LIVE

    @property
    def final_path(self) -> Path:
        return self.path / FINAL

    @property
    def final_tmp_path(self) -> Path:
        return self.path / FINAL_TMP

    @property
    def speakers_path(self) -> Path:
        return self.path / SPEAKERS

    @property
    def marks_path(self) -> Path:
        return self.path / MARKS

    @property
    def echo_dropped_path(self) -> Path:
        return self.path / ECHO_DROPPED

    @property
    def heartbeat_path(self) -> Path:
        return self.path / HEARTBEAT

    @property
    def tags_path(self) -> Path:
        return self.path / TAGS

    @property
    def done_path(self) -> Path:
        return self.path / DONE

    @property
    def audio_dir(self) -> Path:
        return self.path / AUDIO

    def audio_file(self, source: str) -> Path:
        return self.audio_dir / f"{source}.flac"

    @property
    def words_path(self) -> Path:
        """Word timings for the after-Stop pass; lives with the buffered audio."""
        return self.audio_dir / "words.jsonl"

    @property
    def is_done(self) -> bool:
        return self.done_path.exists()

    # -- metadata -------------------------------------------------------
    def read_meta(self) -> dict:
        return read_json(self.meta_path)

    def update_meta(self, **changes) -> dict:
        with self._lock:
            meta = self.read_meta()
            meta.update(changes)
            write_json_atomic(self.meta_path, meta)
            return meta

    def update_meta_settings(self, **changes) -> dict:
        """Change entries of ``meta["settings"]`` (the settings in force for this meeting) without
        touching the others, in one locked read-modify-write."""
        with self._lock:
            meta = self.read_meta()
            meta["settings"] = {**(meta.get("settings") or {}), **changes}
            write_json_atomic(self.meta_path, meta)
            return meta

    def update_meta_models(self, **changes) -> dict:
        """Add entries to ``meta["models"]`` (a model loaded part-way through the meeting) in one locked
        read-modify-write."""
        with self._lock:
            meta = self.read_meta()
            meta["models"] = {**(meta.get("models") or {}), **changes}
            write_json_atomic(self.meta_path, meta)
            return meta

    def read_shared_mic(self) -> SharedMic:
        """The meeting's shared mic stretches as ``meta.json`` has them (``SharedMic.from_meta``)."""
        return SharedMic.from_meta(self.read_meta())

    def write_shared_mic(self, shared: SharedMic) -> dict:
        """Write the meeting's shared mic (``SharedMic.write``) in one locked read-modify-write."""
        with self._lock:
            meta = shared.write(self.read_meta())
            write_json_atomic(self.meta_path, meta)
            return meta

    def drop_meta(self, *keys: str) -> dict:
        """Remove keys from meta.json (e.g. ``error`` when a failed final pass is retried)."""
        with self._lock:
            meta = self.read_meta()
            for key in keys:
                meta.pop(key, None)
            write_json_atomic(self.meta_path, meta)
            return meta

    def add_gap(self, source: str, start_ms: int, end_ms: int, reason: str) -> None:
        with self._lock:
            meta = self.read_meta()
            meta.setdefault("gaps", []).append(
                {"source": source, "start_ms": int(start_ms), "end_ms": int(end_ms), "reason": reason}
            )
            write_json_atomic(self.meta_path, meta)

    def record_mute(self, closed: list[tuple[int, int]], since_ms: int | None) -> dict:
        """Muted periods that have closed (``[(start_ms, end_ms)]``, mic) are added to ``gaps`` with
        reason ``muted``, and ``mic_muted_since_ms`` is set (None when not muted), in one write."""
        with self._lock:
            meta = self.read_meta()
            gaps = meta.setdefault("gaps", [])
            for start, end in closed:
                gaps.append({"source": "mic", "start_ms": int(start), "end_ms": int(end), "reason": MUTED})
            meta["mic_muted_since_ms"] = None if since_ms is None else int(since_ms)
            write_json_atomic(self.meta_path, meta)
            return meta

    def beat(self, at: datetime | None = None) -> None:
        """Refresh heartbeat.json (see ``heartbeat``)."""
        write_heartbeat(self.heartbeat_path, at or now_local())

    # -- speakers (F9) --------------------------------------------------
    def read_speakers(self) -> dict[str, str]:
        try:
            data = read_json(self.speakers_path)
        except (FileNotFoundError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.get("speakers", data).items()} if isinstance(data, dict) else {}

    def write_speakers(self, mapping: dict[str, str]) -> None:
        clean = {k: v.strip() for k, v in mapping.items() if v and v.strip() and v.strip() != k}
        with self._lock:
            try:
                prev = read_json(self.speakers_path)
            except (OSError, ValueError):
                prev = {}
            prev = prev if isinstance(prev, dict) else {}
            data = {"speakers": clean, "updated_at": iso(now_local())}
            last = prev.get("names_changed_at") if isinstance(prev.get("names_changed_at"), str) else None
            if prev.get("speakers") == clean and last:
                data["names_changed_at"] = last
            else:
                data["names_changed_at"] = change_time(last)
            write_json_atomic(self.speakers_path, data)

    def merge_speakers(self, user_names: dict[str, str | None], *, from_tags: bool | None = None,
                       conflicts: list[dict] | None = None) -> dict:
        """Write the user's names into speakers.json, keeping every name for a speaker the user did not
        name (the downstream app may have set it). The file is re-read under the lock immediately before
        the write. A value that is None, empty or equal to the label removes the name for that speaker.
        ``from_tags`` and ``conflicts`` are set when given (the final pass); otherwise conflicts for a
        speaker named here are dropped, as the user has settled them. ``names_changed_at`` moves only
        when a name actually changes (never with the transcript version). Other keys in the file are kept."""
        with self._lock:
            try:
                data = read_json(self.speakers_path)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            speakers = data.get("speakers")
            speakers = {str(k): str(v) for k, v in speakers.items()} if isinstance(speakers, dict) else {}
            before = dict(speakers)
            for label, name in user_names.items():
                name = (name or "").strip()
                if name and name != label:
                    speakers[label] = name
                else:
                    speakers.pop(label, None)
            data["speakers"] = speakers
            if speakers != before:
                prev = data.get("names_changed_at")
                data["names_changed_at"] = change_time(prev if isinstance(prev, str) else None)
            if from_tags is not None:
                data["from_tags"] = bool(from_tags)
            if conflicts is not None:
                data["conflicts"] = conflicts
            elif isinstance(data.get("conflicts"), list):
                data["conflicts"] = [c for c in data["conflicts"]
                                     if isinstance(c, dict) and c.get("speaker") not in user_names]
            data["updated_at"] = iso(now_local())
            write_json_atomic(self.speakers_path, data)
            return data

    # -- completion -----------------------------------------------------
    def mark_done(self) -> None:
        self.done_path.touch()

    def clear_done(self) -> None:
        """The final transcript is about to be replaced: the meeting is no longer done."""
        self.done_path.unlink(missing_ok=True)


def iter_meetings(meetings_dir: Path):
    """Yield MeetingFolder objects for every meeting folder, oldest first."""
    meetings_dir = Path(meetings_dir)
    if not meetings_dir.exists():
        return
    for child in sorted(meetings_dir.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / META).exists():
            yield MeetingFolder(child)


def merge_speakers(folder: MeetingFolder, user_names: dict[str, str | None], *, from_tags: bool | None = None,
                   conflicts: list[dict] | None = None) -> dict:
    """See ``MeetingFolder.merge_speakers``: the user's names win, every other entry is kept."""
    return folder.merge_speakers(user_names, from_tags=from_tags, conflicts=conflicts)
