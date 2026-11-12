"""Model locations and one-time downloads.

All network access in the app happens here, and only when a model is missing.
Once models are installed, recording and finalizing run fully offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

WHISPER_MODELS = ("tiny.en", "base.en", "small.en", "medium.en")
# Tried in order. The second is a copy of the same files kept as assets of this
# project's "whisper-models" GitHub release, for networks that block Hugging Face.
WHISPER_SOURCES = (
    "https://huggingface.co/Systran/faster-whisper-{name}/resolve/main/{file}",
    "https://github.com/BryceAnderson10/huddle-helper/releases/download/whisper-models/faster-whisper-{name}-{file}",
)
# model.bin comes last: whisper_installed() looks for it, so an interrupted
# download is picked up again instead of being mistaken for a complete model.
WHISPER_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")

_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_NAME = "pyannote-segmentation-3-0"
SEGMENTATION_URL = f"{_SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"  # of the archive
# 3D-Speaker ERes2Net, English (WP13). Its cutoffs are diarize.PROFILES[EMBEDDING_NAME]. The model
# shipped before, wespeaker_en_voxceleb_resnet34_LM, is no longer used; its file is left in place.
EMBEDDING_NAME = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k"
# The upstream release tag really is spelled "recongition".
EMBEDDING_URL = f"{_SHERPA}/speaker-recongition-models/{EMBEDDING_NAME}.onnx"
EMBEDDING_SHA256 = "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"  # the release's checksum.txt

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


# Why "Several people on my mic" is unavailable (#55). The model is fetched when Huddle Helper starts, never
# during a meeting.
VOICE_MODEL_MISSING = ("The voice model is not downloaded, so your mic's speech cannot be split into speakers. "
                       "Restart Huddle Helper while online to download it, or run: huddle-helper download-models")


def missing_models(whisper_name: str, need_diarization: bool, root: Path | None = None) -> list[str]:
    missing = []
    if not whisper_installed(whisper_name, root):
        missing.append(f"whisper {whisper_name}")
    if need_diarization and not diarization_installed(root):
        missing.append("diarization")
    return missing


def _download(url: str, dest: Path, progress: Progress | None, sha256: str | None = None) -> None:
    """Download to ``dest``. With ``sha256`` the file is checked before it gets its name; a wrong
    file is deleted and raises OSError, so a damaged or swapped download is never used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"Downloading {url.rsplit('/', 1)[-1]}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            digest.update(chunk)
            fh.write(chunk)
    if sha256 is not None and digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise OSError(f"{url.rsplit('/', 1)[-1]} did not match its checksum; download it again")
    os.replace(tmp, dest)


def install_whisper(name: str, root: Path | None = None, progress: Progress | None = None) -> Path:
    if name not in WHISPER_MODELS:
        raise ValueError(f"Unknown whisper model {name!r}; choose one of {WHISPER_MODELS}")
    target = whisper_dir(name, root)
    if whisper_installed(name, root):
        return target
    if progress:
        progress(f"Downloading whisper model {name}")
    failures = []
    for source in WHISPER_SOURCES:
        host = urllib.parse.urlsplit(source).netloc or source
        if failures and progress:
            progress(f"Downloading whisper model {name} from {host} instead")
        try:
            for filename in WHISPER_FILES:
                if not (target / filename).exists():
                    _download(source.format(name=name, file=filename), target / filename, None)
            return target
        except OSError as exc:  # blocked, offline, or not published there
            log.warning("Could not download whisper model %s from %s: %s", name, host, exc)
            failures.append(f"{host}: {exc}")
    raise OSError(f"Could not download whisper model {name} ({'; '.join(failures)})")


def install_diarization(root: Path | None = None, progress: Progress | None = None) -> None:
    seg = segmentation_model(root)
    if not seg.exists():
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "seg.tar.bz2"
            _download(SEGMENTATION_URL, archive, progress, SEGMENTATION_SHA256)
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(seg.parent.parent, filter="data")
    emb = embedding_model(root)
    if not emb.exists():
        _download(EMBEDDING_URL, emb, progress, EMBEDDING_SHA256)


def install_all(whisper_name: str, need_diarization: bool = True, root: Path | None = None,
                progress: Progress | None = None) -> None:
    install_whisper(whisper_name, root, progress)
    if need_diarization:
        install_diarization(root, progress)
