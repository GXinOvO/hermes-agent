"""Corruption evidence must name the same file generation as an auto-restore."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import state_integrity, update_cmd_maint


def _database(path, value, *, table="sessions", epoch: int | str = 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f'CREATE TABLE "{table}"(value TEXT)')
        db.execute(f'INSERT INTO "{table}" VALUES(?)', (value,))
        db.execute("CREATE TABLE runtime_epoch(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                   "epoch INTEGER NOT NULL CHECK(epoch > 0), instance_id TEXT NOT NULL)")
        db.execute("INSERT INTO runtime_epoch VALUES(1,?, 'fixture')", (epoch,))
        db.commit()


def _damage(path, table="sessions"):
    with closing(sqlite3.connect(path)) as db:
        page = db.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        size = db.execute("PRAGMA page_size").fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(page - 1) * size] = 255
    path.write_bytes(data)


def _swap_on_query(monkeypatch, old, replacement, query):
    connect = sqlite3.connect
    swapped = []

    class SwapOnRead(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith(query) and not swapped:
                replacement.replace(old)
                swapped.append(True)
            return super().execute(sql, *args)

    monkeypatch.setattr(state_integrity.sqlite3, "connect",
                        lambda *args, **kwargs: connect(*args, factory=SwapOnRead, **kwargs))
    return swapped


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("full, table, query", [
    (False, "sessions", 'SELECT 1 FROM "sessions"'),
    (True, "t", "PRAGMA integrity_check"),
])
def test_retired_file_corruption_is_incomplete(tmp_path, monkeypatch, full, table, query):
    old, replacement = tmp_path / "old.db", tmp_path / "new.db"
    _database(old, "corrupt old generation", table=table)
    _damage(old, table)
    _database(replacement, "newest retained transcript", table=table)
    healthy = replacement.read_bytes()
    swapped = _swap_on_query(monkeypatch, old, replacement, query)
    result = state_integrity.check_database(old, full=full)
    assert swapped
    assert result["status"] == "incomplete", result
    if full:
        assert result["full_check"] == "incomplete"
    assert old.read_bytes() == healthy


@pytest.mark.platforms("posix")
def test_update_does_not_restore_healthy_replacement_from_retired_corruption(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    old, replacement = home / "state.db", tmp_path / "new.db"
    _database(old, "corrupt old generation")
    _damage(old)
    _database(replacement, "newest retained transcript")
    snapshot = home / "state-snapshots" / "older" / "state.db"
    _database(snapshot, "older snapshot")
    healthy, snap_bytes = replacement.read_bytes(), snapshot.read_bytes()
    swapped = _swap_on_query(monkeypatch, old, replacement, 'SELECT 1 FROM "sessions"')
    update_cmd_maint._verify_and_restore_one_state_db(home, label="generation fixture")
    assert swapped
    assert old.read_bytes() == healthy
    assert snapshot.read_bytes() == snap_bytes


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("boundary", ["after_check", "maintenance_lock", "epoch_preparation"])
def test_auto_restore_rechecks_generation_inside_maintenance(tmp_path, monkeypatch, boundary):
    from gateway import runtime_ownership
    from hermes_cli import backup_restore

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    old, replacement = home / "state.db", tmp_path / "new.db"
    _database(old, "corrupt old generation")
    _damage(old)
    _database(replacement, "newest retained transcript")
    snapshot = home / "state-snapshots" / "older" / "state.db"
    _database(snapshot, "older snapshot")
    healthy, snap_bytes = replacement.read_bytes(), snapshot.read_bytes()
    changed = []

    def swap():
        if not changed:
            replacement.replace(old)
            changed.append(True)

    if boundary == "after_check":
        actual_check = state_integrity.check_database

        def check_then_swap(*args, **kwargs):
            result = actual_check(*args, **kwargs)
            assert result["status"] == "corrupt"
            swap()
            return result

        monkeypatch.setattr(state_integrity, "check_database", check_then_swap)
    elif boundary == "maintenance_lock":
        actual_reservation = runtime_ownership.exclusive_maintenance

        @contextmanager
        def reservation_then_swap(*args, **kwargs):
            with actual_reservation(*args, **kwargs):
                swap()
                yield

        monkeypatch.setattr(runtime_ownership, "exclusive_maintenance", reservation_then_swap)
    else:
        actual_epoch_source = backup_restore._restore_epoch_source

        @contextmanager
        def epoch_then_swap(*args, **kwargs):
            with actual_epoch_source(*args, **kwargs) as prepared:
                swap()
                yield prepared

        monkeypatch.setattr(backup_restore, "_restore_epoch_source", epoch_then_swap)
    update_cmd_maint._verify_and_restore_one_state_db(home, label="restore generation fixture")
    assert changed
    assert old.read_bytes() == healthy
    assert snapshot.read_bytes() == snap_bytes


def test_auto_restore_same_generation_corruption_still_recovers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    old = home / "state.db"
    _database(old, "corrupt old generation")
    _damage(old)
    snapshot = home / "state-snapshots" / "older" / "state.db"
    _database(snapshot, "recovered transcript")
    snap_bytes = snapshot.read_bytes()
    update_cmd_maint._verify_and_restore_one_state_db(home, label="same generation fixture")
    with closing(sqlite3.connect(old)) as db:
        assert db.execute("SELECT value FROM sessions").fetchall() == [("recovered transcript",)]
    assert snapshot.read_bytes() == snap_bytes
