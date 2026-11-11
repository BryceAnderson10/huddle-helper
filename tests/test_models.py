import pytest

from huddle_helper import models


def _mirror(folder, name="tiny.en"):
    """A fake download source on disk; returns its URL template."""
    folder.mkdir()
    for filename in models.WHISPER_FILES:
        (folder / f"faster-whisper-{name}-{filename}").write_text(f"{folder.name}:{filename}")
    return folder.as_uri() + "/faster-whisper-{name}-{file}"


def test_whisper_download_falls_back_to_the_next_source(tmp_path, monkeypatch):
    blocked = (tmp_path / "blocked").as_uri() + "/{name}/{file}"
    monkeypatch.setattr(models, "WHISPER_SOURCES", (blocked, _mirror(tmp_path / "mirror")))
    messages = []

    target = models.install_whisper("tiny.en", tmp_path / "root", progress=messages.append)

    assert models.whisper_installed("tiny.en", tmp_path / "root")
    assert {p.name: p.read_text() for p in target.iterdir()} == {f: f"mirror:{f}" for f in models.WHISPER_FILES}
    assert len(messages) == 2 and "instead" in messages[1]


def test_whisper_download_prefers_the_first_source(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "WHISPER_SOURCES", (_mirror(tmp_path / "first"), _mirror(tmp_path / "second")))

    target = models.install_whisper("tiny.en", tmp_path / "root")

    assert (target / "model.bin").read_text() == "first:model.bin"


def test_whisper_download_reports_every_source_that_failed(tmp_path, monkeypatch):
    sources = tuple((tmp_path / d).as_uri() + "/{name}/{file}" for d in ("one", "two"))
    monkeypatch.setattr(models, "WHISPER_SOURCES", sources)

    with pytest.raises(OSError, match="Could not download whisper model tiny.en") as err:
        models.install_whisper("tiny.en", tmp_path / "root")

    assert "/one/" in str(err.value) and "/two/" in str(err.value)
    assert not models.whisper_installed("tiny.en", tmp_path / "root")


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._data[self._pos:] if n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_checks_the_checksum(tmp_path, monkeypatch):
    """WP13: a file that does not match its SHA-256 is never given its name."""
    import hashlib

    import pytest

    from huddle_helper import models

    data = b"model bytes"
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda url, timeout=60: _FakeResponse(data))
    good = tmp_path / "good.onnx"
    models._download("https://example.invalid/good.onnx", good, None, hashlib.sha256(data).hexdigest())
    assert good.read_bytes() == data
    bad = tmp_path / "bad.onnx"
    with pytest.raises(OSError, match="checksum"):
        models._download("https://example.invalid/bad.onnx", bad, None, "0" * 64)
    assert not bad.exists() and list(tmp_path.iterdir()) == [good]
    models._download("https://example.invalid/any.onnx", tmp_path / "any.onnx", None)  # no checksum given: as before
    assert (tmp_path / "any.onnx").read_bytes() == data


def test_shipped_fingerprint_model_has_a_profile():
    from huddle_helper import models
    from huddle_helper.diarize import PROFILES

    assert models.EMBEDDING_NAME in PROFILES and models.EMBEDDING_URL.endswith(models.EMBEDDING_NAME + ".onnx")
    assert len(models.EMBEDDING_SHA256) == len(models.SEGMENTATION_SHA256) == 64
