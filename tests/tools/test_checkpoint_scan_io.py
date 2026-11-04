"""Checkpoint scans retain coverage without duplicate directory/metadata reads."""
from collections import Counter
import os
from pathlib import Path

import pytest

from tools import checkpoint_manager as checkpoints


@pytest.mark.platforms("posix")  # Creating symlinks needs additional privileges on Windows.
@pytest.mark.parametrize("kind", ["count", "size"])
def test_scan_lists_each_real_directory_once_and_keeps_link_semantics(tmp_path, monkeypatch, kind):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / ".hidden").write_bytes(b"abc")
    (nested / "data").write_bytes(b"12345")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-visited").write_bytes(b"outside")
    (root / "file-link").symlink_to("nested/data")
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    (root / "cycle").symlink_to(root, target_is_directory=True)
    (root / "broken").symlink_to("missing")
    scans = Counter()
    real_scan = os.scandir

    def scanned(path):
        scans[str(Path(path))] += 1
        return real_scan(path)

    monkeypatch.setattr(os, "scandir", scanned)
    result = checkpoints._dir_file_count(str(root)) if kind == "count" else checkpoints._dir_size_bytes(root)
    assert result == (7 if kind == "count" else 13)
    assert scans == {str(root): 1, str(nested): 1}, "checkpoint scans must not enumerate a directory twice"


def test_size_scan_reads_regular_file_metadata_once(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(4):
        (root / str(index)).write_bytes(b"abc")
    probes = Counter()
    real_stat = os.stat
    real_scan = os.scandir

    def stat(path, *args, **kwargs):
        probes[os.fspath(path)] += 1
        return real_stat(path, *args, **kwargs)

    class Entry:
        def __init__(self, value):
            self.value = value

        def stat(self, *args, **kwargs):
            probes[self.value.path] += 1
            return self.value.stat(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.value, name)

    class Scan:
        def __init__(self, path):
            self.scan = real_scan(path)

        def __enter__(self):
            return (Entry(entry) for entry in self.scan)

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "scandir", Scan)
    assert checkpoints._dir_size_bytes(root) == 12
    assert probes == {str(root / str(index)): 1 for index in range(4)}


@pytest.mark.parametrize("kind", ["count", "size"])
def test_unreadable_subtree_does_not_hide_readable_sibling(tmp_path, monkeypatch, kind):
    root = tmp_path / "tree"
    denied, readable = root / "denied", root / "readable"
    denied.mkdir(parents=True)
    readable.mkdir()
    (denied / "hidden-data").write_bytes(b"unavailable")
    (readable / "data").write_bytes(b"12345")
    real_scan = os.scandir

    def scanned(path):
        if Path(path) == denied:
            raise PermissionError("subtree unavailable")
        return real_scan(path)

    monkeypatch.setattr(os, "scandir", scanned)
    result = checkpoints._dir_file_count(str(root)) if kind == "count" else checkpoints._dir_size_bytes(root)
    assert result == (3 if kind == "count" else 5)


def test_count_stops_at_limit_and_closes_directory(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(8):
        (root / str(index)).write_bytes(b"data")
    real_scan = os.scandir
    closed = []
    yielded = []

    class Scan:
        def __init__(self, path):
            self.scan = real_scan(path)

        def __enter__(self):
            for entry in self.scan:
                yielded.append(entry.name)
                yield entry

        def __exit__(self, *args):
            self.scan.close()
            closed.append(True)

    monkeypatch.setattr(os, "scandir", Scan)
    monkeypatch.setattr(checkpoints, "_MAX_FILES", 3)
    assert checkpoints._dir_file_count(str(root)) == 4
    assert closed == [True]
    assert len(yielded) == 4, "a wide directory must stop reading as soon as the entry limit is exceeded"


@pytest.mark.parametrize("kind", ["count", "size"])
def test_scan_interrupt_closes_active_directory(tmp_path, monkeypatch, kind):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "data").write_bytes(b"data")
    real_scan = os.scandir
    closed = []

    class Scan:
        def __init__(self, path):
            self.scan = real_scan(path)

        def __enter__(self):
            return self

        def __iter__(self):
            return self

        def __next__(self):
            next(self.scan)
            raise KeyboardInterrupt("interrupted enumeration")

        def __exit__(self, *args):
            self.scan.close()
            closed.append(True)

    monkeypatch.setattr(os, "scandir", Scan)
    with pytest.raises(KeyboardInterrupt, match="interrupted enumeration"):
        if kind == "count":
            checkpoints._dir_file_count(str(root))
        else:
            checkpoints._dir_size_bytes(root)
    assert closed == [True]


@pytest.mark.platforms("posix")  # Creating symlinks needs additional privileges on Windows.
def test_scan_root_file_missing_and_symlink_compatibility(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    data = root / "data"
    data.write_bytes(b"abc")
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    for empty in (data, tmp_path / "absent"):
        assert checkpoints._dir_file_count(str(empty)) == 0
        assert checkpoints._dir_size_bytes(empty) == 0
    assert checkpoints._dir_file_count(str(link)) == 1
    assert checkpoints._dir_size_bytes(link) == 3
