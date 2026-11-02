"""Full verification is a separate read-only operation on an offline copy."""
import argparse
from contextlib import closing
import json
import sqlite3


def test_sessions_verify_dispatches_without_opening_active_store(tmp_path, monkeypatch, capsys):
    from hermes_cli.subcommands.sessions import build_sessions_parser, cmd_sessions_lazy

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "offline.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE t(value TEXT UNIQUE)")
        db.execute("INSERT INTO t VALUES('kept')")
        db.commit()
    before = source.read_bytes()
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(), cmd_sessions=cmd_sessions_lazy)
    args = parser.parse_args(["sessions", "verify", "--source", str(source), "--json"])
    assert args.func(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["level"] == "full"
    assert report["status"] == "verified"
    assert report["full_check"] == "passed"
    assert source.read_bytes() == before
    assert not (home / "state.db").exists()


def test_verify_refuses_active_store_and_sidecars(tmp_path, monkeypatch, capsys):
    from hermes_cli.sessions_verify import cmd_verify

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    active = tmp_path / "state.db"
    active.write_bytes(b"\x00" * 4096)
    args = argparse.Namespace(source=active, json=True, budget_seconds=1.0)
    assert cmd_verify(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    offline = tmp_path / "offline.db"
    offline.write_bytes(active.read_bytes())
    (tmp_path / "offline.db-wal").write_bytes(b"uncheckpointed")
    args.source = offline
    assert cmd_verify(args) == 1
    assert "sidecars" in json.loads(capsys.readouterr().out)["message"]


def test_verify_corrupt_offline_copy_is_nonzero_and_does_not_restore(tmp_path, capsys):
    from hermes_cli.sessions_verify import cmd_verify

    source = tmp_path / "corrupt.db"
    source.write_bytes(b"\x00" * 4096)
    before = source.read_bytes()
    args = argparse.Namespace(source=source, json=True, budget_seconds=1.0)
    assert cmd_verify(args) == 1
    assert json.loads(capsys.readouterr().out)["full_check"] == "failed"
    assert source.read_bytes() == before


def test_verify_rejects_hardlink_of_active_store(tmp_path, monkeypatch, capsys):
    from hermes_cli.sessions_verify import cmd_verify

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    active = tmp_path / "state.db"
    with closing(sqlite3.connect(active)) as db:
        db.execute("CREATE TABLE t(value TEXT)")
    alias = tmp_path / "offline-looking.db"
    alias.hardlink_to(active)
    args = argparse.Namespace(source=alias, json=True, budget_seconds=1.0)
    assert cmd_verify(args) == 1
    assert "active profile" in json.loads(capsys.readouterr().out)["message"]


def test_verify_rejects_closed_wal_file_without_sidecars(tmp_path, capsys):
    from hermes_cli.sessions_verify import cmd_verify

    source = tmp_path / "wal.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(value TEXT)")
        db.commit()
    assert not (tmp_path / "wal.db-wal").exists()
    args = argparse.Namespace(source=source, json=True, budget_seconds=1.0)
    assert cmd_verify(args) == 1
    assert "WAL-mode" in json.loads(capsys.readouterr().out)["message"]
