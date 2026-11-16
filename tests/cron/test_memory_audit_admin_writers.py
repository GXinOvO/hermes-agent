import asyncio
import json
import os
from pathlib import Path
import signal
import socket
from types import SimpleNamespace
import zipfile

import pytest

from cron.memory_audit_policy import RunPolicy
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.fixture
def admin_call(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    memories = home / 'memories'
    memories.mkdir(parents=True)
    for name in ('MEMORY.md', 'USER.md'):
        (memories / name).write_text(name + ' original')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    source = tmp_path / 'snapshot'
    source.write_text('restored fixture')
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('memories/USER.md', source.read_bytes())

    def apply(entry):
        if entry == 'cli':
            from hermes_cli.main_agent_cmds import _cmd_memory_reset
            return _cmd_memory_reset(SimpleNamespace(target='all', yes=True))
        if entry == 'web':
            from hermes_cli.web_routers import ops

            async def scoped(profile, action):
                return await asyncio.to_thread(action)

            monkeypatch.setattr(ops, 'destructive_profile', lambda profile, operation: profile)
            monkeypatch.setattr(ops, 'config_scoped_to_thread', scoped)
            return asyncio.run(ops.reset_memory(ops.MemoryReset(target='all')))
        from hermes_cli.backup_restore import _extract_member_atomically, _restore_snapshot_entry
        if entry == 'archive':
            with zipfile.ZipFile(archive) as zf:
                return _extract_member_atomically(zf, 'memories/USER.md', memories / 'USER.md')
        assert entry == 'snapshot'
        return _restore_snapshot_entry('memories/USER.md', source, memories / 'USER.md')

    return home, memories, apply


@pytest.mark.parametrize('entry', ['cli', 'web', 'archive', 'snapshot'])
def test_admin_memory_writers_refuse_audit_context(admin_call, entry):
    _, memories, apply = admin_call
    before = {p.name: p.read_bytes() for p in memories.iterdir()}
    token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
    try:
        with pytest.raises(Exception, match='memory_audit_admin_forbidden'):
            apply(entry)
    finally:
        memory_tool.reset_memory_audit_policy(token)
    assert {name: (memories / name).read_bytes() for name in before} == before


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('lock', ['audit', 'memory', 'user'])
@pytest.mark.parametrize('entry', ['cli', 'web', 'archive', 'snapshot'])
def test_admin_memory_writers_refuse_concurrent_locks(admin_call, entry, lock):
    home, memories, apply = admin_call
    lock_path = home / 'cron' / '.memory-audit.lock' if lock == 'audit' else memories / (
        'MEMORY.md' if lock == 'memory' else 'USER.md')
    before = {name: (memories / name).read_bytes() for name in ('MEMORY.md', 'USER.md')}
    parent, child = socket.socketpair()
    parent.settimeout(8)
    owner = None
    try:
        with MemoryStore._file_lock(lock_path) as fd:
            owner = os.fork()
            if owner == 0:
                parent.close()
                if fd is not None:
                    os.close(fd)
                try:
                    apply(entry)
                    result = 'unexpected_success'
                except BaseException as error:
                    result = str(error)
                child.sendall(result.encode())
                child.close()
                os._exit(0)
            child.close()
            result = parent.recv(4096).decode()
            assert 'memory_audit_admin_busy' in result, result
            waited, status = os.waitpid(owner, 0)
            assert waited == owner and os.waitstatus_to_exitcode(status) == 0
            owner = None
            assert {name: (memories / name).read_bytes() for name in before} == before
    finally:
        parent.close()
        child.close()
        if owner is not None:
            try:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('entry', ['cli', 'web', 'archive', 'snapshot'])
def test_admin_memory_writers_keep_ordinary_behavior(admin_call, entry):
    _, memories, apply = admin_call
    result = apply(entry)
    if entry in ('cli', 'web'):
        assert not (memories / 'MEMORY.md').exists()
        assert not (memories / 'USER.md').exists()
        if entry == 'web':
            assert set(result['deleted']) == {'MEMORY.md', 'USER.md'}
    else:
        assert (memories / 'USER.md').read_text() == 'restored fixture'
        assert (memories / 'MEMORY.md').read_text() == 'MEMORY.md original'


@pytest.mark.parametrize('entry', ['cli', 'web', 'archive', 'snapshot'])
def test_other_profile_audit_does_not_block_admin_writer(admin_call, tmp_path, entry):
    _, memories, apply = admin_call
    with MemoryStore._file_lock(tmp_path / 'other-home' / 'cron' / '.memory-audit.lock'):
        apply(entry)
    assert (memories / 'USER.md').exists() == (entry not in ('cli', 'web'))


@pytest.mark.parametrize('entry', ['cli', 'web'])
@pytest.mark.parametrize('invalid', ['directory', 'symlink'])
def test_admin_reset_validates_all_targets_before_deleting(admin_call, tmp_path, entry, invalid):
    _, memories, apply = admin_call
    user = memories / 'USER.md'
    user.unlink()
    if invalid == 'directory':
        user.mkdir()
    else:
        user.symlink_to(tmp_path / 'absent')
    with pytest.raises(Exception, match='memory_audit_admin_non_regular_target'):
        apply(entry)
    assert (memories / 'MEMORY.md').read_text() == 'MEMORY.md original'


@pytest.mark.parametrize('entry', ['archive', 'snapshot'])
def test_admin_restore_requires_exact_readback(admin_call, monkeypatch, entry):
    from hermes_cli import backup_restore

    _, memories, apply = admin_call
    if entry == 'archive':
        monkeypatch.setattr(backup_restore, '_extract_member_target_locked', lambda *args: None)
    else:
        monkeypatch.setattr(backup_restore, '_restore_snapshot_entry_target_locked', lambda *args, **kwargs: True)
    with pytest.raises(OSError, match='memory_audit_admin_restore_readback_failed'):
        apply(entry)
    assert (memories / 'USER.md').read_text() == 'USER.md original'


@pytest.mark.parametrize('relative', ['memories/USER.md.lock', 'memories/MEMORY.md.lock', 'cron/.memory-audit.lock.lock'])
def test_archive_restore_cannot_replace_memory_lock_inode(admin_call, tmp_path, relative):
    from hermes_cli.backup_restore import _extract_member_atomically

    home, _, _ = admin_call
    target = home / relative
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b'live lock')
    before = target.stat()
    archive = tmp_path / 'locks.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr(relative, b'archived lock')
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(OSError, match='memory_audit_admin_lock_file'):
            _extract_member_atomically(zf, relative, target)
    assert target.read_bytes() == b'live lock'
    assert target.stat().st_ino == before.st_ino
