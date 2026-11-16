from contextlib import ExitStack
import json
import os
from pathlib import Path
import zipfile

import pytest

from cron.memory_audit_policy import RunPolicy
from hermes_cli import backup, backup_restore
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.fixture
def restore_alias(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    memories = home / 'memories'
    memories.mkdir(parents=True)
    (home / 'cron').mkdir()
    for name in ('MEMORY.md', 'USER.md'):
        (memories / name).write_bytes((name + ' original').encode())
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    source = tmp_path / 'replacement'
    source.write_bytes(b'restored fixture')
    archive = tmp_path / 'restore.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('innocent.txt', source.read_bytes())

    def restore(entry, destination):
        if entry == 'archive':
            with zipfile.ZipFile(archive) as zf:
                return backup_restore._extract_member_atomically(zf, 'innocent.txt', destination)
        if entry == 'import':
            with zipfile.ZipFile(archive) as zf:
                result = backup._import_members(zf, ['innocent.txt'], '', home, 1)
            assert result[0] == 0 and len(result[2]) == 1, result
            raise OSError(result[2][0])
        if entry == 'quick_snapshot':
            snapshot = backup._quick_snapshot_root(home) / 'fixture'
            snapshot.mkdir(parents=True)
            (snapshot / 'innocent.txt').write_bytes(source.read_bytes())
            (snapshot / 'manifest.json').write_text(json.dumps({'files': {'innocent.txt': len(source.read_bytes())}}))
            return backup.restore_quick_snapshot('fixture', home)
        assert entry == 'snapshot'
        return backup_restore._restore_snapshot_entry('innocent.txt', source, destination)

    return home, source, restore


@pytest.mark.parametrize('entry', ['archive', 'snapshot', 'import', 'quick_snapshot'])
@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
@pytest.mark.parametrize('protection', ['none', 'context', 'locks'])
def test_restore_alias_cannot_bypass_memory_admission(restore_alias, entry, target, protection):
    home, _, restore = restore_alias
    destination = home / 'memories' / target
    alias = home / 'innocent.txt'
    alias.symlink_to(destination)
    before = destination.read_bytes()
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        elif protection == 'locks':
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        with pytest.raises(OSError, match='memory_audit_admin_alias'):
            restore(entry, alias)
        assert destination.read_bytes() == before
        assert alias.is_symlink()


@pytest.mark.parametrize('entry', ['archive', 'snapshot', 'import', 'quick_snapshot'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md.lock', 'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['none', 'context', 'locks'])
def test_restore_alias_cannot_replace_live_memory_lock(restore_alias, entry, target, protection):
    home, _, restore = restore_alias
    destination = home / target
    destination.write_bytes(b'live lock')
    before = destination.stat()
    alias = home / 'innocent.txt'
    alias.symlink_to(destination)
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        elif protection == 'locks':
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        with pytest.raises(OSError, match='memory_audit_admin_lock_file'):
            restore(entry, alias)
        assert destination.read_bytes() == b'live lock'
        assert destination.stat().st_ino == before.st_ino


@pytest.mark.parametrize('entry', ['archive', 'snapshot'])
@pytest.mark.parametrize('protection', ['context', 'locks'])
def test_ordinary_config_alias_remains_restorable(restore_alias, entry, protection):
    home, source, restore = restore_alias
    config = home / 'config.txt'
    config.write_bytes(b'original config')
    alias = home / 'innocent.txt'
    alias.symlink_to(config)
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
        restore(entry, alias)
    assert config.read_bytes() == source.read_bytes()
    assert alias.is_symlink()


@pytest.mark.parametrize('entry', ['archive', 'snapshot'])
@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
def test_directory_alias_cannot_hide_memory_destination(restore_alias, entry, target):
    home, _, restore = restore_alias
    directory = home / 'other'
    directory.symlink_to(home / 'memories', target_is_directory=True)
    destination = home / 'memories' / target
    original = destination.read_bytes()
    with pytest.raises(OSError, match='memory_audit_admin_alias'):
        restore(entry, directory / target)
    assert destination.read_bytes() == original


@pytest.mark.parametrize('entry', ['snapshot', 'quick_snapshot'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['none', 'context', 'locks'])
def test_snapshot_hardlink_alias_cannot_modify_memory_or_lock(restore_alias, entry, target, protection, caplog):
    home, _, restore = restore_alias
    destination = home / target
    if not destination.exists():
        destination.write_bytes(b'live lock')
    alias = home / 'innocent.txt'
    os.link(destination, alias)
    original = destination.read_bytes()
    inode = destination.stat().st_ino
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        elif protection == 'locks':
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        assert restore(entry, alias) is False
        assert 'snapshot_restore_hardlink_target' in caplog.text
        assert destination.read_bytes() == original
        assert destination.stat().st_ino == inode


def test_archive_hardlink_alias_replaces_only_the_alias(restore_alias):
    home, source, restore = restore_alias
    destination = home / 'memories' / 'USER.md'
    alias = home / 'innocent.txt'
    os.link(destination, alias)
    original = destination.read_bytes()
    restore('archive', alias)
    assert destination.read_bytes() == original
    assert alias.read_bytes() == source.read_bytes()
    assert alias.stat().st_ino != destination.stat().st_ino


def test_profile_home_symlink_keeps_canonical_memory_restore(restore_alias, tmp_path):
    home, source, restore = restore_alias
    alias = tmp_path / 'linked-home'
    alias.symlink_to(home, target_is_directory=True)
    restore('archive', alias / 'memories' / 'USER.md')
    assert (home / 'memories' / 'USER.md').read_bytes() == source.read_bytes()


@pytest.mark.parametrize('entry', ['snapshot', 'quick_snapshot'])
@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
@pytest.mark.parametrize('protection', ['context', 'locks'])
def test_snapshot_directory_alias_cannot_redirect_copy(restore_alias, tmp_path, entry, target, protection, caplog):
    home, _, _ = restore_alias
    source = tmp_path / target
    source.write_bytes(b'restored fixture')
    alias = home / target
    alias.symlink_to(home / 'memories', target_is_directory=True)
    destination = home / 'memories' / target
    original = destination.read_bytes()
    snapshot = backup._quick_snapshot_root(home) / 'directory-fixture'
    snapshot.mkdir(parents=True)
    (snapshot / target).write_bytes(source.read_bytes())
    (snapshot / 'manifest.json').write_text(json.dumps({'files': {target: source.stat().st_size}}))
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        if entry == 'snapshot':
            result = backup_restore._restore_snapshot_entry(target, source, alias)
        else:
            result = backup.restore_quick_snapshot('directory-fixture', home)
        assert destination.read_bytes() == original
        assert result is False
        assert 'snapshot_restore_directory_target' in caplog.text
