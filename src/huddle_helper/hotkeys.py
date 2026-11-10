"""Global hotkeys (LIVE_VIEW "Bookmarks", plan 4.7): combo parsing, the AltGr check, and a thread that
owns the ``RegisterHotKey`` registrations. No Tk.

    combo = parse("Ctrl+Alt+B")                 # Combo(mods, vk); ValueError says what is wrong
    combo.text                                    # canonical "Ctrl+Alt+B"
    thread = HotkeyThread({"bookmark": "Ctrl+Alt+B", ...}, on_press)   # on_press(name) on that thread
    thread.start(); thread.results["bookmark"].ok; thread.stop()
    check_combo("Ctrl+Alt+Q")                     # ComboCheck: valid?, the AltGr warning and suggestion

``RegisterHotKey`` is per thread: the registrations belong to the thread that made them and its
message loop receives ``WM_HOTKEY``, so the thread has its own loop and is stopped by posting
``WM_QUIT``. Registrations die with the thread, even if Stop fails. ``MOD_NOREPEAT`` makes a held key
one press. Windows delivers the press without giving our process the focus, which is the point.

``on_press`` runs on the hotkey thread and must only hand over (the tray puts a call on its queue).
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_QUIT, WM_HOTKEY, WM_USER = 0x0012, 0x0312, 0x0400
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# Action name -> default combo. An empty string in Settings turns an action off.
DEFAULTS = {
    "bookmark": "Ctrl+Alt+B",
    "decision": "Ctrl+Alt+D",
    "action": "Ctrl+Alt+A",
    "question": "Ctrl+Alt+Q",
    "mute": "Ctrl+Alt+M",
}
ACTION_LABELS = {"bookmark": "Bookmark", "decision": "Decision", "action": "Action item",
                 "question": "Question", "mute": "Mute me in the recording"}

_MOD_NAMES = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))
_MOD_ALIASES = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
                "windows": "Win", "super": "Win", "meta": "Win"}
_KEYS = {
    "Space": 0x20, "Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B, "Backspace": 0x08, "Insert": 0x2D,
    "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
}
_KEY_ALIASES = {"escape": "Esc", "return": "Enter", "del": "Delete", "ins": "Insert", "pgup": "PageUp",
                "pgdn": "PageDown", "pageup": "PageUp", "pagedown": "PageDown", "spacebar": "Space"}
_KEY_BY_NAME = {k.lower(): (k, v) for k, v in _KEYS.items()}


@dataclass(frozen=True)
class Combo:
    mods: int
    vk: int
    text: str  # canonical, e.g. "Ctrl+Alt+Shift+B"

    @property
    def has_ctrl_alt(self) -> bool:
        return bool(self.mods & MOD_CONTROL) and bool(self.mods & MOD_ALT)


def parse(text: str) -> Combo:
    """``"Ctrl+Alt+B"`` -> Combo. Modifiers in any order and case (Ctrl, Alt, Shift, Win); one key:
    a letter, a digit, F1-F24 or a named key (Space, Enter, Esc, arrows...). At least one modifier is
    required, or the hotkey would take that key away from every program. ValueError explains."""
    parts = [p.strip() for p in str(text).split("+")]
    if len(parts) < 2 or not all(parts):
        raise ValueError("Use at least one modifier and a key, for example Ctrl+Alt+B.")
    key = parts[-1]
    mods, names = 0, []
    for p in parts[:-1]:
        name = _MOD_ALIASES.get(p.lower())
        if name is None:
            raise ValueError(f"“{p}” is not a modifier (use Ctrl, Alt, Shift or Win).")
        bit = dict(_MOD_NAMES)[name]
        if mods & bit:
            raise ValueError(f"{name} is listed twice.")
        mods |= bit
        names.append(name)
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk, shown = ord(key.upper()), key.upper()
    elif key.upper().startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk, shown = 0x70 + int(key[1:]) - 1, f"F{int(key[1:])}"
    else:
        found = _KEY_BY_NAME.get(_KEY_ALIASES.get(key.lower(), key).lower())
        if found is None:
            if key.lower() in _MOD_ALIASES:
                raise ValueError("The last part must be the key itself, not a modifier.")
            raise ValueError(f"“{key}” is not a key I know (use a letter, digit, F1-F24, Space, Enter, Esc…).")
        shown, vk = found
    ordered = [n for n, _ in _MOD_NAMES if mods & dict(_MOD_NAMES)[n]]
    return Combo(mods, vk, "+".join([*ordered, shown]))


def normalize(text: str) -> str:
    """Canonical text of a combo, ``""`` for off. ValueError for a combo that does not parse."""
    return parse(text).text if text and text.strip() else ""


# -- AltGr -------------------------------------------------------------------------------------
def altgr_char(combo: Combo | str, hkl: int | None = None) -> str | None:
    """The character this combo types on the current keyboard layout when Ctrl+Alt is AltGr, or None.

    On a German layout AltGr+Q is ``@``; a registered Ctrl+Alt+Q would swallow it. Asked of
    ``ToUnicodeEx`` with flag 0x4 (do not change the keyboard state, so dead keys are not disturbed).
    Control characters (what plain US/UK layouts give for Ctrl+letter) do not count. Only combos with
    both Ctrl and Alt can be AltGr. ``hkl`` defaults to the foreground window's layout.
    """
    c = parse(combo) if isinstance(combo, str) else combo
    if not c.has_ctrl_alt:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.MapVirtualKeyExW.restype = wintypes.UINT
    if hkl is None:
        tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        hkl = user32.GetKeyboardLayout(tid) or user32.GetKeyboardLayout(0)
    scan = user32.MapVirtualKeyExW(c.vk, 0, ctypes.c_void_p(hkl))  # MAPVK_VK_TO_VSC
    state = (ctypes.c_ubyte * 256)()
    for vk in (0x11, 0xA2, 0x12, 0xA5):  # Ctrl, LCtrl, Alt, RAlt: how Windows reports AltGr
        state[vk] = 0x80
    if c.mods & MOD_SHIFT:
        state[0x10] = state[0xA0] = 0x80
    buf = ctypes.create_unicode_buffer(8)
    user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int,
                                   wintypes.UINT, ctypes.c_void_p]
    n = user32.ToUnicodeEx(c.vk, scan, state, buf, len(buf), 0x4, ctypes.c_void_p(hkl))
    if n == 0:
        return None
    ch = buf.value[:abs(n)][:1]
    return ch if ch and ch.isprintable() and not ch.isspace() else None


@dataclass(frozen=True)
class ComboCheck:
    """What Settings says about one hotkey text, before anything is registered."""
    text: str  # as typed
    canonical: str = ""  # "" when off or invalid
    error: str = ""  # why it does not parse
    altgr: str | None = None  # the character AltGr+key types on this layout
    suggestion: str = ""  # the same with Shift added, if that is free of the problem

    @property
    def off(self) -> bool:
        return not self.text.strip()

    @property
    def valid(self) -> bool:
        return not self.error

    @property
    def warning(self) -> str:
        if not self.altgr:
            return ""
        s = (f" Try {self.suggestion}." if self.suggestion else "")
        return (f"On this keyboard layout Ctrl+Alt is AltGr, and {self.canonical} types “{self.altgr}”. "
                f"As a hotkey it would swallow that character.{s}")


def check_combo(text: str, char_for: Callable[[Combo], str | None] = altgr_char) -> ComboCheck:
    """Parse ``text`` and run the AltGr check (``char_for`` is injectable for tests and non-Windows)."""
    if not text or not text.strip():
        return ComboCheck(text or "")
    try:
        combo = parse(text)
    except ValueError as exc:
        return ComboCheck(text, error=str(exc))
    try:
        ch = char_for(combo)
    except Exception:  # noqa: BLE001 - a failed layout lookup must not break the dialog
        log.exception("AltGr check failed for %s", combo.text)
        ch = None
    suggestion = ""
    if ch and not combo.mods & MOD_SHIFT:
        *mods, key = combo.text.split("+")
        shifted = parse("+".join([*mods, "Shift", key]))
        try:
            if char_for(shifted) is None:
                suggestion = shifted.text
        except Exception:  # noqa: BLE001
            pass
    return ComboCheck(text, combo.text, "", ch, suggestion)


def find_duplicates(bindings: dict[str, str]) -> dict[str, str]:
    """action -> the other action that has the same combo (only combos that parse; later ones are flagged)."""
    seen: dict[str, str] = {}
    dup: dict[str, str] = {}
    for name, text in bindings.items():
        try:
            key = normalize(text)
        except ValueError:
            continue
        if not key:
            continue
        if key in seen:
            dup[name] = seen[key]
        else:
            seen[key] = name
    return dup


# -- the thread ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Registration:
    combo: str  # canonical text, "" when off
    ok: bool
    error: str = ""  # human reason when not ok


class HotkeyThread:
    """Registers the bindings on its own thread and calls ``on_press(name)`` for each press."""

    def __init__(self, bindings: dict[str, str], on_press: Callable[[str], None]):
        self.bindings = dict(bindings)
        self.on_press = on_press
        self.results: dict[str, Registration] = {}
        self._ready = threading.Event()
        self._tid = 0
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)

    def start(self, timeout: float = 3.0) -> dict[str, Registration]:
        self._thread.start()
        self._ready.wait(timeout)
        return self.results

    @property
    def failed(self) -> dict[str, Registration]:
        return {k: r for k, r in self.results.items() if not r.ok and r.error != "off"}

    def stop(self, timeout: float = 3.0) -> None:
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        msg = wintypes.MSG()
        ids: dict[int, str] = {}
        try:
            user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, 0)  # create this thread's queue
            self._tid = kernel32.GetCurrentThreadId()
            for i, (name, text) in enumerate(self.bindings.items(), 1):
                if not text or not text.strip():
                    self.results[name] = Registration("", False, "off")
                    continue
                try:
                    combo = parse(text)
                except ValueError as exc:
                    self.results[name] = Registration("", False, str(exc))
                    continue
                if user32.RegisterHotKey(None, i, combo.mods | MOD_NOREPEAT, combo.vk):
                    ids[i] = name
                    self.results[name] = Registration(combo.text, True)
                else:
                    err = ctypes.get_last_error()
                    why = ("already in use by another program" if err == ERROR_HOTKEY_ALREADY_REGISTERED
                           else f"Windows refused it (error {err})")
                    self.results[name] = Registration(combo.text, False, why)
                    log.warning("Hotkey %s for %s: %s", combo.text, name, why)
            self._ready.set()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    try:
                        self.on_press(ids[msg.wParam])
                    except Exception:  # noqa: BLE001
                        log.exception("Hotkey handler failed")
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)
            self._ready.set()


def probe(bindings: dict[str, str]) -> dict[str, Registration]:
    """Which of these combos could be registered right now? Registers them for an instant and lets go
    (for the Settings tab when no recording holds them)."""
    t = HotkeyThread(bindings, lambda name: None)
    results = dict(t.start())
    t.stop()
    return results
