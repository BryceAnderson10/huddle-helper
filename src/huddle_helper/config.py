"""User settings, stored as JSON in %APPDATA%\\HuddleHelper\\settings.json."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import bookmark_types, hotkeys as hk, paths

log = logging.getLogger(__name__)


# Bump when a tuning default changes; ``_MIGRATIONS`` lists the keys whose saved values
# from older versions are dropped so the new default applies. (Those keys aren't in the
# Settings dialog; the dialog saves every field, so old defaults would otherwise stick.)
SETTINGS_VERSION = 4
_MIGRATIONS = {
    2: ("live_speaker_threshold",),  # 0.6 -> 0.75, measured on real meeting audio (Test #2)
    3: ("live_min_new_speaker_s",),  # 1.5 -> 2.0, short opening lines (Test #3)
    4: ("final_voice_threshold",),  # 0.65 -> 0.70 with the gated fold, keeps Test #4's 28 s speaker apart
}


@dataclass
class Settings:
    settings_version: int = SETTINGS_VERSION

    # Devices (F11). None means "follow the Windows default device".
    mic_device: str | None = None
    output_device: str | None = None
    meetings_dir: str = ""

    # Transcript window (WP4): open it when a recording starts. Additive: older files lack the key and
    # get the default, so no SETTINGS_VERSION bump.
    open_window_on_start: bool = True

    # Transcription (F4, F13: English-only models in v1).
    whisper_model: str = "base.en"
    beam_size: int = 1
    cpu_threads: int = 0  # 0 lets CTranslate2 decide

    # Speaker labels (F5).
    diarize_system: bool = True
    diarize_mic: bool = False
    live_speaker_threshold: float = 0.75
    live_min_new_speaker_s: float = 2.0
    final_cluster_threshold: float = 0.6  # offline diarizer; now only decides where lines split
    final_voice_threshold: float = 0.70  # whole-meeting voice clustering for final labels

    # Echo handling.
    echo_dedupe: bool = True

    # Audio buffer (F10, F14).
    keep_audio: bool = False
    auto_purge_days: int = 0  # 0 disables auto-purge

    # Custom vocabulary (WP3): terms Whisper should expect, one per entry. See vocab.py.
    vocabulary: list[str] = field(default_factory=list)

    # Clips (WP9b, L19): "Play clips on", the playback device's name (never an index); None follows the
    # Windows default output. Not the capture's "Meeting audio from" (output_device). Additive.
    clip_output_device: str | None = None

    # Global hotkeys (WP5), registered only while recording: action -> combo such as "Ctrl+Alt+B".
    # "" turns one off. Additive like open_window_on_start: a missing key means the default combo.
    hotkeys: dict[str, str] = field(default_factory=lambda: dict(hk.DEFAULTS))

    # User-defined bookmark types (WP9c, L28), at most four: [{"id": "risk", "name": "Risk", "hotkey": ""}].
    # See bookmark_types.py. Additive: a missing key means none.
    custom_bookmark_types: list[dict] = field(default_factory=list)

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
        saved_version = int(clean.get("settings_version", 1))
        for version, keys in _MIGRATIONS.items():
            if saved_version < version:
                for key in keys:
                    clean.pop(key, None)
        vocab = clean.get("vocabulary")
        if not (isinstance(vocab, list) and all(isinstance(t, str) for t in vocab)):
            clean.pop("vocabulary", None)
        saved = clean.get("hotkeys")
        if isinstance(saved, dict):  # actions the file lacks keep their default; unknown ones are dropped
            clean["hotkeys"] = {**hk.DEFAULTS, **{k: v for k, v in saved.items()
                                                       if k in hk.DEFAULTS and isinstance(v, str)}}
        else:
            clean.pop("hotkeys", None)
        if "custom_bookmark_types" in clean:
            clean["custom_bookmark_types"] = bookmark_types.clean(clean["custom_bookmark_types"])
        clean["settings_version"] = SETTINGS_VERSION
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
