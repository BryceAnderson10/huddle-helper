# PyInstaller spec for Huddle Helper (one-folder build).
# Build with:  powershell -File packaging\build.ps1
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "sherpa_onnx", "pyaudiowpatch", "soxr",
            "soundfile", "pystray", "pycaw", "comtypes", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["pystray._win32", "huddle_helper.tray"]

a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["torch", "matplotlib", "scipy", "pandas", "IPython", "pytest"],
    noarchive=False,
)

# Some wheels carry an old MSVC runtime (msvcp140.dll 14.16). onnxruntime and
# CTranslate2 are built with a newer toolset and crash (access violation in
# std::mutex) when that old copy is loaded first, so ship the system's
# up-to-date runtime instead.
import os

_sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_vc_runtime = {"msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll",
               "concrt140.dll"}
_fixed = []
for name, path, kind in a.binaries:
    base = os.path.basename(name).lower()
    if base in _vc_runtime and os.path.dirname(name) == "":
        system_copy = os.path.join(_sys32, base)
        if os.path.exists(system_copy):
            path = system_copy
    _fixed.append((name, path, kind))
a.binaries = _fixed

pyz = PYZ(a.pure)

app = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HuddleHelper",
    console=False,
    icon=None,
)
cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="huddle-helper-cli",
    console=True,
)
coll = COLLECT(app, cli, a.binaries, a.datas, name="HuddleHelper")
