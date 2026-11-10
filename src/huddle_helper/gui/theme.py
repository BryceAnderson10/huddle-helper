"""Colours, fonts and DPI setup of the Transcript window (plan 4.5, "Theme"). No Tk import."""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

FONT_FAMILY = "Segoe UI"
FONT_SIZE = 10
FONT_SIZE_SMALL = 9

TEXT = "#111827"
MUTED = "#6b7280"
RECORDING = "#dc2626"
BUSY = "#d97706"
IDLE = "#6e7681"
ERROR = "#7c3aed"

MUTE_BAR_BG, MUTE_BAR_FG = "#fef3c7", "#92400e"
MUTE_REMIND_BG, MUTE_REMIND_FG = "#fbbf24", "#451a03"  # the bar for a minute after a "still muted" reminder
NOTICE_BG, NOTICE_FG = "#eff6ff", "#1e40af"
FAILED_BG, FAILED_FG = "#fef2f2", "#991b1b"
CHANGED_BG = "#fef9c3"
PLAYING_BG = "#dbeafe"
SELECTED_BG = "#e5e7eb"
CHIP_NEW_BG = "#fef3c7"
GAP_FG = MUTED
PLAY_ON, PLAY_OFF = "#1d4ed8", "#b6bcc6"  # the play control: usable / greyed out (the tooltip says why)
PLAY_GLYPH, STOP_GLYPH = "▶", "■"
WARN_BG, WARN_FG = MUTE_BAR_BG, MUTE_BAR_FG  # one-off notices such as the headphone warning
MARK_BG = "#f3f4f6"

SPEAKER_COLORS = ("#1d4ed8", "#b45309", "#047857", "#7c3aed", "#be123c", "#0e7490", "#a16207", "#4b5563")
YOU_COLOR = TEXT
KIND_COLORS = {"bookmark": "#b45309", "decision": "#047857", "action": "#1d4ed8", "question": "#7c3aed"}
KIND_SYMBOLS = {"bookmark": "⚑", "decision": "✔", "action": "☐", "question": "?"}
CUSTOM_COLOR, CUSTOM_SYMBOL = "#475569", "◆"  # every user-defined bookmark type (L28): neutral slate, a diamond

DOT_COLORS = {"recording": RECORDING, "busy": BUSY, "idle": IDLE, "error": ERROR}

HIGHLIGHT_SECONDS = 10  # changed lines stay highlighted this long after the final swap (L3)


def speaker_color(index: int, is_you: bool = False) -> str:
    """Colour for the ``index``-th distinct person (order of first appearance); mic lines are 'You'."""
    return YOU_COLOR if is_you else SPEAKER_COLORS[index % len(SPEAKER_COLORS)]


def make_dpi_aware() -> bool:
    """Per-system DPI awareness, before the first Tk window, so text is sharp at 125-150% scaling.
    Returns False when it could not be set (already set, or not Windows)."""
    try:
        return ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0
    except (AttributeError, OSError):
        return False
