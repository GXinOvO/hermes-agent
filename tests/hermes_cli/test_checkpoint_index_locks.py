"""The read-only checkpoint lock entry reports blockage without recovery claims."""
import argparse
import json

import pytest

from hermes_cli import checkpoints
from tools import checkpoint_manager as cm


def test_locks_entry_reports_exact_generation_without_project_scan(tmp_path, monkeypatch, capsys):
    base = tmp_path / "checkpoints"
    store = base / "store"
    indexes = store / "indexes"
    indexes.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    key = cm._project_hash(str(project))
    meta = store / "projects" / (key + ".json")
    meta.parent.mkdir()
    meta.write_text(json.dumps({"workdir": str(project)}))
    lock = indexes / (key + ".lock")
    lock.write_bytes(b"retained writer data")
    identity = lock.stat()
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)

    def no_scan(*args, **kwargs):
        raise AssertionError("lock diagnosis must not scan project contents or run Git")

    monkeypatch.setattr(cm, "_dir_file_count", no_scan)
    monkeypatch.setattr(cm, "_run_git", no_scan)
    parser = argparse.ArgumentParser()
    checkpoints.register_cli(parser)
    args = parser.parse_args(["locks", "--json"])
    assert args.func(args) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["base"] == str(base)
    assert len(info["locks"]) == 1
    row = info["locks"][0]
    assert row["project_hash"] == key
    assert row["workdir"] == str(project)
    assert row["identity"]["inode"] == identity.st_ino
    assert row["identity"]["mtime_ns"] == identity.st_mtime_ns
    assert row["ownership"] == "unknown"
    assert row["recovery_allowed"] is False
    assert lock.read_bytes() == b"retained writer data"
    assert lock.stat().st_ino == identity.st_ino
    assert meta.read_text() == json.dumps({"workdir": str(project)})


@pytest.mark.parametrize("metadata_kind", ["mismatch", "malformed", "symlink"])
def test_untrusted_metadata_does_not_invent_owner_or_follow_link(tmp_path, monkeypatch, capsys, metadata_kind):
    base = tmp_path / "checkpoints"
    store = base / "store"
    (store / "indexes").mkdir(parents=True)
    key = "a" * 16
    lock = store / "indexes" / (key + ".lock")
    lock.write_bytes(b"writer evidence")
    meta = store / "projects" / (key + ".json")
    meta.parent.mkdir()
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"workdir": "/foreign"}))
    if metadata_kind == "symlink":
        try:
            meta.symlink_to(foreign)
        except OSError:
            pytest.skip("symlinks unavailable")
    elif metadata_kind == "malformed":
        meta.write_text("invalid json")
    else:
        meta.write_text(foreign.read_text())
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    parser = argparse.ArgumentParser()
    checkpoints.register_cli(parser)
    args = parser.parse_args(["locks", "--json"])
    assert args.func(args) == 0
    row = json.loads(capsys.readouterr().out)["locks"][0]
    assert row["workdir"] is None
    assert row["metadata_error"]
    assert row["ownership"] == "unknown"
    assert row["recovery_allowed"] is False
    assert foreign.read_text() == json.dumps({"workdir": "/foreign"})
    assert lock.read_bytes() == b"writer evidence"


@pytest.mark.parametrize("recursive_decoder", [False, True], ids=["native", "stdlib_recursive"])
def test_deep_json_metadata_isolated_from_another_valid_lock(tmp_path, monkeypatch, capsys, recursive_decoder):
    if recursive_decoder:
        # Python 3.14's native decoder is iterative. Exercise the stdlib's
        # recursive decoder used as a compatibility control, not a 3.14 fault.
        from json.scanner import py_make_scanner
        decoder = json.JSONDecoder()
        decoder.scan_once = py_make_scanner(decoder)
        monkeypatch.setattr(json, "_default_decoder", decoder)
    base = tmp_path / "checkpoints"
    store = base / "store"
    (store / "indexes").mkdir(parents=True)
    (store / "projects").mkdir()
    keys = []
    for name in ("bad", "good"):
        project = tmp_path / name
        project.mkdir()
        key = cm._project_hash(str(project))
        keys.append(key)
        (store / "indexes" / (key + ".lock")).write_bytes(b"retained evidence")
        metadata = "[" * 2000 + "0" + "]" * 2000 if name == "bad" else json.dumps({"workdir": str(project)})
        assert len(metadata.encode()) < 65536
        (store / "projects" / (key + ".json")).write_text(metadata)
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    parser = argparse.ArgumentParser()
    checkpoints.register_cli(parser)
    args = parser.parse_args(["locks", "--json"])
    assert args.func(args) == 0
    info = json.loads(capsys.readouterr().out)
    rows = {row["project_hash"]: row for row in info["locks"]}
    assert len(rows) == 2
    assert rows[keys[0]]["metadata_error"]
    assert rows[keys[0]]["workdir"] is None
    assert rows[keys[1]]["workdir"] == str(tmp_path / "good")
    assert all(row["ownership"] == "unknown" and not row["recovery_allowed"] for row in rows.values())
    assert all((store / "indexes" / (key + ".lock")).read_bytes() == b"retained evidence" for key in keys)


def test_locks_entry_missing_store_does_not_create_it(tmp_path, monkeypatch, capsys):
    base = tmp_path / "absent"
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    parser = argparse.ArgumentParser()
    checkpoints.register_cli(parser)
    args = parser.parse_args(["locks", "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["locks"] == []
    assert not base.exists()
