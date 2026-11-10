"""Recycle Bin delete through the Windows shell (``SHFileOperationW``), by ctypes: no new dependency.

``recycle(paths)`` sends files or folders to the Recycle Bin without any dialog; ``has_recycle_bin``
says whether a path is on a drive that has one (a network share or removable stick has none, and
Windows would then delete for good, so callers use a permanent delete and say so).

Windows only. Off Windows ``recycle`` raises ``OSError`` and ``has_recycle_bin`` is False.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040  # move to the Recycle Bin instead of deleting
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000  # report a delete that would be permanent
FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING

DRIVE_FIXED = 3


class SHFILEOPSTRUCTW(ctypes.Structure):
    # The header packs this structure on 32-bit Windows only.
    _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def double_null_list(paths: list[str]) -> str:
    """``a\\0b\\0\\0``: what ``pFrom`` wants. (ctypes adds the last terminator when it copies the string.)"""
    return "\0".join(paths) + "\0"


def _absolute(path: Path | str) -> str:
    # The shell does not accept forward slashes or "..".
    return os.path.normpath(os.path.abspath(str(path)))


def recycle(paths: list[Path | str]) -> None:
    """Move these files or folders to the Recycle Bin. Raises ``OSError`` if the shell reports an error,
    the user's operation was aborted, or any path is still there afterwards. Missing paths are an error."""
    if not paths:
        return
    if sys.platform != "win32":
        raise OSError("The Recycle Bin is only available on Windows")
    absolute = [_absolute(p) for p in paths]
    for p in absolute:
        if not os.path.lexists(p):
            raise FileNotFoundError(p)
    buf = ctypes.create_unicode_buffer(double_null_list(absolute), len("\0".join(absolute)) + 2)
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.pTo = None
    op.fFlags = FLAGS
    op.lpszProgressTitle = None
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(f"The Recycle Bin operation failed (error 0x{rc & 0xFFFFFFFF:X})")
    if op.fAnyOperationsAborted:
        raise OSError("The Recycle Bin operation was cancelled")
    left = [p for p in absolute if os.path.lexists(p)]
    if left:
        raise OSError(f"Could not move to the Recycle Bin: {', '.join(left)}")


def has_recycle_bin(path: Path | str) -> bool:
    """True when ``path`` is on a fixed local drive. Network shares (UNC or mapped), removable drives
    and CD/RAM drives have none; the answer errs towards "no", which makes the caller say "permanently"."""
    if sys.platform != "win32":
        return False
    full = _absolute(path)
    if full.startswith("\\\\"):
        return False
    root = os.path.splitdrive(full)[0] + "\\"
    if len(root) < 3:
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED
    except (AttributeError, OSError):
        return False
