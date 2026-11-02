"""Per-profile failure isolation must retain later profiles and explicit receipts."""
from contextlib import closing
from pathlib import Path
import sqlite3

from hermes_cli import backup, update_cmd, update_cmd_maint, update_receipt
from tests.hermes_cli import test_update_state_generation as fixture


def test_bad_snapshot_epoch_does_not_skip_later_profile(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    for home in (first, second):
        fixture._database(home / "state.db", "corrupt current")
        fixture._damage(home / "state.db")
        fixture._database(home / "state-snapshots" / "older" / "state.db", "restored sibling",
                          epoch="unknown" if home == first else 10)
    before = (first / "state.db").read_bytes()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: first)
    monkeypatch.setattr(backup, "_sibling_profile_homes", lambda home: [("second", second)])
    update_receipt.begin_update_receipt()
    update_cmd_maint._verify_and_restore_state_dbs_post_update()
    with closing(sqlite3.connect(second / "state.db")) as db:
        assert db.execute("SELECT value FROM sessions").fetchall() == [("restored sibling",)]
    assert (first / "state.db").read_bytes() == before
    current = update_receipt._current.get()
    assert current is not None
    assert any(check["profile"] == "profile second" for check in current.data["state_db_checks"])
    assert any("unknown" in str(followup) for followup in current.data["followups"])


def test_missing_database_has_incomplete_receipt_and_never_restores(tmp_path, monkeypatch):
    restores = []
    monkeypatch.setattr(update_cmd_maint, "_restore_state_db_from_snapshot", lambda *a, **kw: restores.append(a))
    update_receipt.begin_update_receipt()
    update_cmd_maint._verify_and_restore_one_state_db(tmp_path, label="missing fixture")
    current = update_receipt._current.get()
    assert current is not None
    result = current.data["state_db_checks"][-1]
    assert result["profile"] == "missing fixture"
    assert result["status"] == "missing" and result["level"] == "structural"
    assert result["full_check"] == "deferred"
    assert current.data["followups"]
    assert not restores and not (tmp_path / "state.db").exists()


def test_metadata_unavailable_has_incomplete_receipt(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    fixture._database(path, "kept")
    before = path.read_bytes()
    stat = Path.stat

    def unavailable(p, *args, **kwargs):
        if p == path:
            raise PermissionError("fixture storage unavailable")
        return stat(p, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unavailable)
    update_receipt.begin_update_receipt()
    update_cmd_maint._verify_and_restore_one_state_db(tmp_path, label="storage fixture")
    current = update_receipt._current.get()
    assert current is not None
    result = current.data["state_db_checks"][-1]
    assert result["profile"] == "storage fixture" and result["status"] == "incomplete"
    assert result["level"] == "structural" and result["full_check"] == "deferred"
    assert "fixture storage unavailable" in result["message"]
    assert current.data["followups"] and path.read_bytes() == before
