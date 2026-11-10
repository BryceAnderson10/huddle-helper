# PyInstaller spec for Huddle Helper (one-folder build).
# Build with:  powershell -File packaging\build.ps1
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "sherpa_onnx", "pyaudiowpatch", "soxr",
            "soundfile", "pystray", "comtypes", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["pystray._win32", "huddle_helper.tray"]
# The Transcript window is imported lazily from tray.py (and the replay driver from cli.py).
# tray.py imports hotkeys.py inside a function (hotkeys exist only while recording); marks.py and
# audio/gate.py are imported at module level by the recorder and controller. Listed for certainty.
hiddenimports += ["huddle_helper.hotkeys", "huddle_helper.marks", "huddle_helper.audio.gate",
                  "huddle_helper.bookmark_types", "huddle_helper.mute_reminder"]
hiddenimports += ["huddle_helper.gui", "huddle_helper.gui.window", "huddle_helper.gui.transcript_view",
                  "huddle_helper.gui.dialogs", "huddle_helper.gui.logic", "huddle_helper.gui.theme",
                  "huddle_helper.session", "huddle_helper.replay", "huddle_helper.attendees",
                  "huddle_helper.gui.attendee_logic",
                  "huddle_helper.leveling", "huddle_helper.outputs", "huddle_helper.playsequence",
                  "huddle_helper.storage", "huddle_helper.gui.storage_dialog", "huddle_helper.gui.meetings_view",
                  "huddle_helper.gui.meetings_logic", "huddle_helper.gui.confirm"]

a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # av: replaced by a stub in rthook_no_av.py. hf_xet: only used by huggingface_hub
    # downloads, and models are fetched with urllib (huddle_helper.models).
    runtime_hooks=[os.path.join(SPECPATH, "rthook_no_av.py")],
    excludes=["torch", "matplotlib", "scipy", "pandas", "IPython", "pytest", "av", "hf_xet"],
    noarchive=False,
)

# Some wheels carry an old MSVC runtime (msvcp140.dll 14.16). onnxruntime and
# CTranslate2 are built with a newer toolset and crash (access violation in
# std::mutex) when that old copy is loaded first, so ship the system's
# up-to-date runtime instead.
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
