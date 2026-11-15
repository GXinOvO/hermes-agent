import sqlite3

import pytest

from agent import verification_evidence as evidence


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_VERIFY_ON_STOP', '1')
    return home / 'verification_evidence.db', workspace


def test_missing_readonly_ledger_does_not_create_a_directory_or_database(ledger):
    path, workspace = ledger
    with pytest.raises(sqlite3.OperationalError):
        evidence.verification_status(session_id='s', cwd=workspace, read_only=True)
    assert not path.parent.exists()


@pytest.mark.parametrize('edited', [False, True])
def test_readonly_status_reuses_native_calculation_without_schema_or_meta_writes(ledger, edited):
    path, workspace = ledger
    evidence.record_terminal_result(command='pytest tests/test_one.py', cwd=workspace,
                                    session_id='s', exit_code=0, output='passed')
    if edited:
        evidence.mark_workspace_edited(session_id='s', cwd=workspace, paths=['changed.py'])
    expected = evidence.verification_status(session_id='s', cwd=workspace)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value='readonly-sentinel' WHERE key='schema_version'")
    before = path.read_bytes()

    actual = evidence.verification_status(session_id='s', cwd=workspace, read_only=True)

    assert actual == expected
    assert actual['status'] == ('stale' if edited else 'passed')
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == 'readonly-sentinel'


def test_default_status_reader_remains_compatible_and_can_initialize_its_ledger(ledger):
    path, workspace = ledger
    assert evidence.verification_status(session_id='s', cwd=workspace)['status'] == 'unverified'
    assert path.exists()
