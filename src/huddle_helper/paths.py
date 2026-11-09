"""Well-known locations on disk."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "HuddleHelper"


def _env_path(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    """Roaming settings folder (%APPDATA%\\HuddleHelper)."""
    override = os.environ.get("HUDDLE_HELPER_CONFIG_DIR")
    if override:
        return Path(override)
    return _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME


def data_dir() -> Path:
    """Local data folder for models and logs (%LOCALAPPDATA%\\HuddleHelper)."""
    override = os.environ.get("HUDDLE_HELPER_DATA_DIR")
    if override:
        return Path(override)
    return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME


def models_dir() -> Path:
    """Where downloaded models are installed."""
    return data_dir() / "models"


def bundled_models_dir() -> Path | None:
    """Models shipped next to the packaged executable, if any (read-only)."""
    import sys

    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "models"
        if candidate.exists():
            return candidate
    return None


def logs_dir() -> Path:
    return data_dir() / "logs"


def default_meetings_dir() -> Path:
    """Default output folder from the output spec: %USERPROFILE%\\MeetingCompanion\\meetings."""
    return _env_path("USERPROFILE", Path.home()) / "MeetingCompanion" / "meetings"
