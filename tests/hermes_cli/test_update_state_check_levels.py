"""The update checks readability, not every row/index in the history store."""
from contextlib import closing
import sqlite3


def test_update_check_does_not_scan_rows_and_records_deferred_full_check(tmp_path, monkeypatch, capsys):
    from hermes_cli import backup, update_cmd_maint, update_receipt

    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, content TEXT)")
        db.execute("INSERT INTO sessions VALUES('kept', 'history')")
        db.commit()
    original = path.read_bytes()
    queries = []
    connect = backup.sqlite3.connect

    def trace_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(backup.sqlite3, "connect", trace_connect)
    update_receipt.begin_update_receipt()
    update_cmd_maint._verify_and_restore_one_state_db(tmp_path, label="default home")
    assert not any("integrity_check" in query.lower() for query in queries), queries
    current = update_receipt._current.get()
    assert current is not None
    checks = current.data["state_db_checks"]
    assert checks[-1]["level"] == "structural"
    assert checks[-1]["status"] == "readable"
    assert checks[-1]["full_check"] == "deferred"
    output = capsys.readouterr().out.lower()
    assert "full integrity check deferred" in output
    assert path.read_bytes() == original


def test_incomplete_check_cannot_trigger_snapshot_restore(tmp_path, monkeypatch, capsys):
    from hermes_cli import state_integrity, update_cmd_maint

    (tmp_path / "state.db").write_bytes(b"not consumed by this injected check")
    monkeypatch.setattr(state_integrity, "check_database", lambda path: {
        "level": "structural", "status": "incomplete", "full_check": "deferred",
        "message": "database is locked", "elapsed_seconds": 0.01,
    })
    restores = []
    monkeypatch.setattr(update_cmd_maint, "_restore_state_db_from_snapshot", lambda *args: restores.append(args))
    update_cmd_maint._verify_and_restore_one_state_db(tmp_path, label="locked home")
    output = capsys.readouterr().out
    assert not restores
    assert "incomplete" in output and "no restore attempted" in output
    assert "is corrupted" not in output


def test_structural_failure_detects_zeroed_header_without_changing_bytes(tmp_path, monkeypatch, capsys):
    from hermes_cli import update_cmd_maint

    path = tmp_path / "state.db"
    path.write_bytes(b"\x00" * 4096)
    before = path.read_bytes()
    update_cmd_maint._verify_and_restore_one_state_db(tmp_path, label="zeroed home")
    assert "is corrupted" in capsys.readouterr().out
    assert path.read_bytes() == before
