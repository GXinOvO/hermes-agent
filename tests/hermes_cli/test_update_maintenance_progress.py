"""Maintenance stages must stay visible while blocking work runs."""
from contextlib import closing
import sqlite3
import threading

from tests.waiting import wait_until


def test_database_check_publishes_stage_and_heartbeat_until_it_finishes(tmp_path, monkeypatch, capsys):
    from hermes_cli import state_integrity, update_cmd_maint, update_stage

    with closing(sqlite3.connect(tmp_path / "state.db")) as db:
        db.execute("CREATE TABLE sessions(id TEXT)")
    entered, release = threading.Event(), threading.Event()
    published = []
    monkeypatch.setattr(update_stage, "publish_stage", published.append)
    check = state_integrity.check_database

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return check(*args, **kwargs)

    monkeypatch.setattr(state_integrity, "check_database", blocked)
    thread = threading.Thread(target=update_cmd_maint._verify_and_restore_one_state_db,
                              args=(tmp_path,), kwargs={"label": "test home"})
    thread.start()
    try:
        assert entered.wait(10)
        assert any("test home" in stage and "state.db" in stage for stage in published)
        from hermes_cli import update_progress
        monkeypatch.setattr(update_progress, "HEARTBEAT_SECONDS", 0.02)
        # A separate stage uses the public progress owner so interval is captured
        # before waiting, and the real update guard above proves the wiring.
        with update_progress.maintenance_stage("blocked fixture"):
            wait_until(lambda: any("blocked fixture" in stage and "still running" in stage
                                   for stage in published), timeout=3, what="maintenance heartbeat")
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    output = capsys.readouterr().out
    assert "elapsed" in output
    assert any("test home" in stage and "finished" in stage for stage in published)


def test_progress_stops_on_exception_without_a_late_success_message(monkeypatch):
    import pytest
    from hermes_cli import update_progress, update_stage

    published = []
    monkeypatch.setattr(update_stage, "publish_stage", published.append)
    monkeypatch.setattr(update_progress, "HEARTBEAT_SECONDS", 0.02)
    with pytest.raises(RuntimeError, match="injected failure"):
        with update_progress.maintenance_stage("fails"):
            raise RuntimeError("injected failure")
    assert published[-1].startswith("fails — stopped without completion")
    assert not any(t.name == "update-maintenance-progress" and t.is_alive() for t in threading.enumerate())


def test_progress_starts_before_database_metadata_access(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import update_cmd_maint, update_progress, update_stage
    from tests.hermes_cli import test_update_state_generation as fixture

    path = tmp_path / "state.db"
    fixture._database(path, "kept")
    entered, release = threading.Event(), threading.Event()
    published = []
    stat = Path.stat

    def blocked_stat(p, *args, **kwargs):
        if p == path:
            entered.set()
            assert release.wait(10)
        return stat(p, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocked_stat)
    monkeypatch.setattr(update_stage, "publish_stage", published.append)
    monkeypatch.setattr(update_progress, "HEARTBEAT_SECONDS", 0.01)
    worker = threading.Thread(target=update_cmd_maint._verify_and_restore_one_state_db,
                              args=(tmp_path,), kwargs={"label": "metadata fixture"})
    worker.start()
    try:
        assert entered.wait(10)
        wait_until(lambda: any("metadata fixture" in stage and "still running" in stage for stage in published),
                   timeout=2, what="metadata wait heartbeat")
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert not any(t.name == "update-maintenance-progress" and t.is_alive() for t in threading.enumerate())


def test_progress_covers_snapshot_validation_after_corruption(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import backup, update_cmd_maint, update_progress, update_stage
    from tests.hermes_cli import test_update_state_generation as fixture

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    fixture._database(home / "state.db", "damaged")
    fixture._damage(home / "state.db")
    source = home / "state-snapshots" / "older" / "state.db"
    fixture._database(source, "restored")
    entered, release = threading.Event(), threading.Event()
    published = []
    verify = backup.verify_sqlite_integrity

    def blocked_verify(path, *args, **kwargs):
        if path == source:
            entered.set()
            assert release.wait(10)
        return verify(path, *args, **kwargs)

    monkeypatch.setattr(backup, "verify_sqlite_integrity", blocked_verify)
    monkeypatch.setattr(update_stage, "publish_stage", published.append)
    monkeypatch.setattr(update_progress, "HEARTBEAT_SECONDS", 0.01)
    worker = threading.Thread(target=update_cmd_maint._verify_and_restore_one_state_db,
                              args=(home,), kwargs={"label": "restore fixture"})
    worker.start()
    try:
        assert entered.wait(10)
        wait_until(lambda: any("restore fixture" in stage and "still running" in stage for stage in published),
                   timeout=2, what="snapshot validation heartbeat")
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    with closing(sqlite3.connect(home / "state.db")) as db:
        assert db.execute("SELECT value FROM sessions").fetchall() == [("restored",)]
