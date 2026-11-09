# PyInstaller runtime hook: stand in for PyAV, which the build leaves out.
#
# faster-whisper imports ``av`` at load but only calls it to decode audio files.
# Huddle Helper always hands Whisper numpy arrays, so the 67 MB of FFmpeg DLLs
# are never used.
import sys
import types


def _missing(name):
    # AttributeError, so hasattr() and the import system's own probes keep working.
    raise AttributeError(f"PyAV is not bundled with Huddle Helper (av.{name}); pass audio as a numpy array")


_stub = types.ModuleType("av")
_stub.__getattr__ = _missing
sys.modules.setdefault("av", _stub)
