"""User settings, stored as JSON in %APPDATA%\\HuddleHelper\\settings.json."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


@dataclass
class Settings:
    # Devices (F11). None means "follow the Windows default device".
    mic_device: str | None = None
    output_device: str | None = None
    meetings_dir: str = ""

    # Transcription (F4, F13: English-only models in v1).
    whisper_model: str = "base.en"
    beam_size: int = 1
    cpu_threads: int = 0  # 0 lets CTranslate2 decide

    # Speaker labels (F5).
    diarize_system: bool = True
    diarize_mic: bool = False
    live_speaker_threshold: float = 0.6
    final_cluster_threshold: float = 0.6

    # Echo handling.
    echo_dedupe: bool = True

    # Audio buffer (F10, F14).
    keep_audio: bool = False
    auto_purge_days: int = 0  # 0 disables auto-purge

    # Segmentation.
    vad_threshold: float = 0.5
    min_silence_ms: int = 500
    max_utterance_s: float = 12.0

    def resolved_meetings_dir(self) -> Path:
        return Path(self.meetings_dir) if self.meetings_dir else paths.default_meetings_dir()

    @property
    def needs_diarization(self) -> bool:
        return self.diarize_system or self.diarize_mic

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    def to_dict(self) -> dict:
        return asdict(self)


def settings_path() -> Path:
    return paths.config_dir() / "settings.json"


def load(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return Settings.from_dict(json.load(fh))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not read settings from %s (%s); using defaults", path, exc)
        return Settings()


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, indent=2)
    os.replace(tmp, path)
