"""Coverage and failure classes of the update-specific read-only checker."""
from contextlib import closing
import sqlite3

import pytest

from hermes_cli import state_integrity


def _database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE t(value TEXT UNIQUE)")
        db.executemany("INSERT INTO t VALUES(?)", [(str(i),) for i in range(100)])
        db.commit()


@pytest.mark.parametrize("full, expected", [(False, "readable"), (True, "verified")])
def test_real_database_reports_exact_coverage(tmp_path, full, expected):
    path = tmp_path / "database.db"
    _database(path)
    before = path.read_bytes()
    report = state_integrity.check_database(path, full=full)
    assert report["status"] == expected
    assert report["full_check"] == ("passed" if full else "deferred")
    assert path.read_bytes() == before


def test_database_read_error_is_not_reported_as_corruption(tmp_path, monkeypatch):
    path = tmp_path / "database.db"
    _database(path)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(state_integrity.sqlite3, "connect", fail)
    report = state_integrity.check_database(path)
    assert report["status"] == "incomplete"
    assert path.read_bytes() == before


def test_full_check_cooperative_deadline_returns_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "database.db"
    _database(path)
    ticks = iter([0.0, 10.0, 11.0, 12.0])
    monkeypatch.setattr(state_integrity.time, "monotonic", lambda: next(ticks, 12.0))
    report = state_integrity.check_database(path, full=True, budget_seconds=1.0)
    assert report["status"] == "incomplete"
    assert report["full_check"] == "incomplete"


def test_full_check_detects_damaged_table_pages(tmp_path):
    path = tmp_path / "database.db"
    _database(path)
    with closing(sqlite3.connect(path)) as db:
        page = db.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(page - 1) * page_size] = 255
    path.write_bytes(data)
    assert state_integrity.check_database(path)["status"] == "readable"
    full = state_integrity.check_database(path, full=True)
    assert full["status"] == "corrupt" and full["full_check"] == "failed"
    assert path.read_bytes() == data


@pytest.mark.platforms("posix")
def test_full_check_rejects_source_replacement_during_scan(tmp_path, monkeypatch):
    path = tmp_path / "database.db"
    replacement = tmp_path / "replacement.db"
    _database(path)
    _database(replacement)
    connect = sqlite3.connect

    class SwappedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            if sql == "PRAGMA integrity_check":
                replacement.replace(path)
            return cursor

    def open_and_swap(*args, **kwargs):
        return connect(*args, factory=SwappedConnection, **kwargs)

    monkeypatch.setattr(state_integrity.sqlite3, "connect", open_and_swap)
    report = state_integrity.check_database(path, full=True)
    assert report["status"] == "incomplete", report
    assert report["full_check"] == "incomplete"


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert state_integrity.check_database(path)["status"] == "missing"
    assert not path.exists()
