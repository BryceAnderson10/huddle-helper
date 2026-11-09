"""Meeting folder layout and metadata (F7).

Layout (see docs/OUTPUT_SPEC.md):

    <meetings>/<meeting_id>/
        meta.json               start time, devices, models, status
        transcript.jsonl        live, append-only
        transcript.final.jsonl  written after Stop (via a temp name + rename)
        speakers.json           optional speaker id -> display name
        DONE                    empty marker, created last
        audio/                  buffered audio, deleted unless Keep audio is on
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from . import SCHEMA_VERSION, __version__

META = "meta.json"
LIVE = "transcript.jsonl"
FINAL = "transcript.final.jsonl"
FINAL_TMP = "transcript.final.jsonl.tmp"
SPEAKERS = "speakers.json"
DONE = "DONE"
AUDIO = "audio"

STATUS_RECORDING = "recording"
STATUS_PROCESSING = "processing"
STATUS_COMPLETE = "complete"
STATUS_INTERRUPTED = "interrupted"
STATUS_FAILED = "failed"


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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


class MeetingFolder:
    """A single meeting's folder plus its metadata."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

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

    def add_gap(self, source: str, start_ms: int, end_ms: int, reason: str) -> None:
        with self._lock:
            meta = self.read_meta()
            meta.setdefault("gaps", []).append(
                {"source": source, "start_ms": int(start_ms), "end_ms": int(end_ms), "reason": reason}
            )
            write_json_atomic(self.meta_path, meta)

    # -- speakers (F9) --------------------------------------------------
    def read_speakers(self) -> dict[str, str]:
        try:
            data = read_json(self.speakers_path)
        except (FileNotFoundError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.get("speakers", data).items()} if isinstance(data, dict) else {}

    def write_speakers(self, mapping: dict[str, str]) -> None:
        clean = {k: v.strip() for k, v in mapping.items() if v and v.strip() and v.strip() != k}
        write_json_atomic(self.speakers_path, {"speakers": clean, "updated_at": iso(now_local())})

    # -- completion -----------------------------------------------------
    def mark_done(self) -> None:
        self.done_path.touch()


def iter_meetings(meetings_dir: Path):
    """Yield MeetingFolder objects for every meeting folder, oldest first."""
    meetings_dir = Path(meetings_dir)
    if not meetings_dir.exists():
        return
    for child in sorted(meetings_dir.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / META).exists():
            yield MeetingFolder(child)
