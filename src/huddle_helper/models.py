"""Model locations and one-time downloads.

All network access in the app happens here, and only when a model is missing.
Once models are installed, recording and finalizing run fully offline.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

WHISPER_MODELS = ("tiny.en", "base.en", "small.en", "medium.en")
_WHISPER_REPOS = "https://huggingface.co/Systran/faster-whisper"
# model.bin comes last: whisper_installed() looks for it, so an interrupted
# download is picked up again instead of being mistaken for a complete model.
WHISPER_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")

_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_NAME = "pyannote-segmentation-3-0"
SEGMENTATION_URL = f"{_SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_NAME = "wespeaker_en_voxceleb_resnet34_LM"
# The upstream release tag really is spelled "recongition".
EMBEDDING_URL = f"{_SHERPA}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"

Progress = Callable[[str], None]


def _locate(rel: Path, root: Path | None) -> Path:
    """Explicit root, else the bundled copy if present, else the user models folder."""
    if root is not None:
        return root / rel
    bundled = paths.bundled_models_dir()
    if bundled is not None and (bundled / rel).exists():
        return bundled / rel
    return paths.models_dir() / rel


def whisper_dir(name: str, root: Path | None = None) -> Path:
    return _locate(Path("whisper") / f"faster-whisper-{name}", root)


def segmentation_model(root: Path | None = None) -> Path:
    return _locate(Path("diarization") / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx", root)


def embedding_model(root: Path | None = None) -> Path:
    return _locate(Path("diarization") / f"{EMBEDDING_NAME}.onnx", root)


def whisper_installed(name: str, root: Path | None = None) -> bool:
    d = whisper_dir(name, root)
    return (d / "model.bin").exists() and (d / "config.json").exists()


def diarization_installed(root: Path | None = None) -> bool:
    return segmentation_model(root).exists() and embedding_model(root).exists()


def missing_models(whisper_name: str, need_diarization: bool, root: Path | None = None) -> list[str]:
    missing = []
    if not whisper_installed(whisper_name, root):
        missing.append(f"whisper {whisper_name}")
    if need_diarization and not diarization_installed(root):
        missing.append("diarization")
    return missing


def _download(url: str, dest: Path, progress: Progress | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"Downloading {url.rsplit('/', 1)[-1]}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1 << 20)
    os.replace(tmp, dest)


def install_whisper(name: str, root: Path | None = None, progress: Progress | None = None) -> Path:
    if name not in WHISPER_MODELS:
        raise ValueError(f"Unknown whisper model {name!r}; choose one of {WHISPER_MODELS}")
    target = whisper_dir(name, root)
    if whisper_installed(name, root):
        return target
    if progress:
        progress(f"Downloading whisper model {name}")
    for filename in WHISPER_FILES:
        if not (target / filename).exists():
            _download(f"{_WHISPER_REPOS}-{name}/resolve/main/{filename}", target / filename, None)
    return target


def install_diarization(root: Path | None = None, progress: Progress | None = None) -> None:
    seg = segmentation_model(root)
    if not seg.exists():
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "seg.tar.bz2"
            _download(SEGMENTATION_URL, archive, progress)
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(seg.parent.parent, filter="data")
    emb = embedding_model(root)
    if not emb.exists():
        _download(EMBEDDING_URL, emb, progress)


def install_all(whisper_name: str, need_diarization: bool = True, root: Path | None = None,
                progress: Progress | None = None) -> None:
    install_whisper(whisper_name, root, progress)
    if need_diarization:
        install_diarization(root, progress)
