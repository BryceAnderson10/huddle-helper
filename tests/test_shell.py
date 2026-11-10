"""Recycle Bin delete through the shell (WP6). The real-bin test sends a uniquely named scratch folder from
pytest's temp folder to the Recycle Bin, then removes exactly that entry from the bin again."""

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from huddle_helper import shell

windows = pytest.mark.skipif(sys.platform != "win32", reason="Recycle Bin is Windows only")


def test_path_list_is_double_null_terminated():
    assert shell.double_null_list(["a", "b"]) == "a\0b\0"
    assert shell.FLAGS == 0x40 | 0x10 | 0x04 | 0x400 | 0x4000


@windows
def test_has_recycle_bin_on_a_fixed_drive_but_not_on_a_share(tmp_path):
    assert shell.has_recycle_bin(tmp_path) is True
    assert shell.has_recycle_bin(r"\\server\share\meetings") is False


def purge_bin_entries(marker: str, drive_root: str) -> int:
    """Remove from the Recycle Bin only the entries whose original path contains ``marker`` (this test's
    own uniquely named scratch items). Nothing else in the bin is touched."""
    removed = 0
    try:
        sid_dirs = [d for d in (Path(drive_root) / "$Recycle.Bin").iterdir() if d.is_dir()]
    except OSError:
        return 0
    for sid in sid_dirs:
        try:
            infos = list(sid.glob("$I*"))
        except OSError:
            continue
        for info in infos:
            try:
                raw = info.read_bytes()
                offset = 28 if raw[:8] == b"\x02" + b"\0" * 7 else 24  # version 2 / version 1 header
                if marker not in raw[offset:].decode("utf-16-le", errors="ignore"):
                    continue
                data = info.with_name("$R" + info.name[2:])
                if data.is_dir():
                    shutil.rmtree(data, ignore_errors=True)
                elif data.exists():
                    data.unlink()
                info.unlink()
                removed += 1
            except OSError:
                continue
    return removed


@windows
def test_recycle_removes_a_folder_and_files_from_where_they_were(tmp_path):
    marker = "huddle-helper-test-recycle-" + uuid.uuid4().hex
    folder = tmp_path / marker
    (folder / "audio").mkdir(parents=True)
    (folder / "meta.json").write_text("{}", encoding="utf-8")
    (folder / "audio" / "mic.flac").write_bytes(b"x")
    single = tmp_path / (marker + ".txt")
    single.write_text("x", encoding="utf-8")
    try:
        shell.recycle([folder, single])
        assert not folder.exists() and not single.exists()
    finally:
        purge_bin_entries(marker, os.path.splitdrive(str(tmp_path))[0] + "\\")


@windows
def test_recycle_reports_a_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        shell.recycle([tmp_path / "nope"])
    shell.recycle([])  # nothing to do
