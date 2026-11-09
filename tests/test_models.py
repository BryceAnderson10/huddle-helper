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
