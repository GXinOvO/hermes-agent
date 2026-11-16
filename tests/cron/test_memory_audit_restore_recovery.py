from contextlib import ExitStack
import json
import os

import pytest

from cron.memory_audit_policy import RunPolicy
from hermes_cli import backup
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
@pytest.mark.parametrize('protection', ['context', 'locks'])
def test_cron_recovery_cannot_write_memory_alias(tmp_path, monkeypatch, target, alias_kind, protection):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: home / 'memories')
    destination = home / target
    original = json.dumps({'jobs': []}).encode()
    destination.write_bytes(original)
    inode = destination.stat().st_ino
    alias = home / 'cron' / 'jobs.json'
    if alias_kind == 'symlink':
        alias.symlink_to(destination)
    else:
        os.link(destination, alias)
    source = backup._quick_snapshot_root(home) / 'fixture' / 'cron' / 'jobs.json'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'jobs': [{'id': 'synthetic-job'}]}))
    with ExitStack() as stack:
        if protection == 'context':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        result = backup.restore_cron_jobs_if_emptied('fixture', home)
        assert destination.read_bytes() == original
        assert destination.stat().st_ino == inode
        assert result is None
