from contextlib import ExitStack, closing
import json
import os
import sqlite3
import zipfile

import pytest

from cron.memory_audit_policy import RunPolicy
from hermes_cli import backup, backup_restore
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.fixture
def database_restore(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    (home / 'cron').mkdir()
    for name in ('MEMORY.md', 'USER.md'):
        (home / 'memories' / name).touch()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: home / 'memories')
    source = tmp_path / 'replacement.db'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE synthetic_sentinel(value TEXT)')
        db.execute('INSERT INTO synthetic_sentinel VALUES(?)', ('restore-sentinel',))
        db.commit()
    archive = tmp_path / 'restore.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('config.yaml', 'synthetic_restore_control: true\n')
        zf.writestr('innocent.db', source.read_bytes())
    snapshot = backup._quick_snapshot_root(home) / 'fixture'
    snapshot.mkdir(parents=True)
    (snapshot / 'innocent.db').write_bytes(source.read_bytes())
    (snapshot / 'manifest.json').write_text(json.dumps({'files': {'innocent.db': source.stat().st_size}}))

    def restore(entry):
        if entry == 'import':
            with zipfile.ZipFile(archive) as zf:
                assert backup._validate_backup_zip(zf)[0] is True
                return backup._import_members(zf, ['innocent.db'], '', home, 1)
        assert entry == 'quick_snapshot'
        return backup.restore_quick_snapshot('fixture', home)

    return home, source, restore


@pytest.mark.parametrize('entry', ['import', 'quick_snapshot'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['context', 'locks'])
@pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
def test_database_alias_cannot_write_memory_or_lock(database_restore, entry, target, protection, alias_kind, caplog):
    home, _, restore = database_restore
    destination = home / target
    destination.touch()
    alias = home / 'innocent.db'
    if alias_kind == 'symlink':
        alias.symlink_to(destination)
    else:
        os.link(destination, alias)
    original = destination.read_bytes()
    inode = destination.stat().st_ino
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        error = None
        try:
            result = restore(entry)
        except OSError as exc:
            result, error = None, str(exc)
        assert destination.read_bytes() == original
        assert destination.stat().st_ino == inode
        assert alias.is_symlink() == (alias_kind == 'symlink')
        if entry == 'import':
            assert error is None and result is not None
            assert result[0] == 0 and len(result[2]) == 1, result
            if alias_kind == 'symlink':
                expected = 'memory_audit_admin_lock_file' if target.endswith('.lock') else 'memory_audit_admin_alias'
                assert expected in caplog.text or expected in result[2][0]
            else:
                assert 'snapshot_restore_hardlink_target' in caplog.text
        elif alias_kind == 'symlink':
            assert error == ('memory_audit_admin_lock_file' if target.endswith('.lock') else 'memory_audit_admin_alias')
        else:
            assert result is False
            assert 'snapshot_restore_hardlink_target' in caplog.text


@pytest.mark.parametrize('entry', ['import', 'quick_snapshot'])
@pytest.mark.parametrize('symlink', [False, True])
def test_normal_database_restore_preserves_destination(database_restore, entry, symlink):
    home, _, restore = database_restore
    destination = home / ('normal.db' if symlink else 'innocent.db')
    destination.touch()
    inode = destination.stat().st_ino
    if symlink:
        (home / 'innocent.db').symlink_to(destination)
    result = restore(entry)
    if entry == 'import':
        assert result[0] == 1 and result[2] == [], result
    else:
        assert result is True
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute('SELECT value FROM synthetic_sentinel').fetchone() == ('restore-sentinel',)
    assert destination.stat().st_ino == inode
    assert (home / 'innocent.db').is_symlink() == symlink


@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
def test_direct_database_restore_cannot_write_memory_or_lock(database_restore, target, caplog):
    home, source, _ = database_restore
    destination = home / target
    destination.touch()
    original = destination.read_bytes()
    assert backup_restore._safe_restore_db(source, destination) is False
    assert destination.read_bytes() == original
    expected = 'memory_audit_admin_lock_file' if target.endswith('.lock') else 'memory_audit_admin_database_target'
    assert expected in caplog.text
