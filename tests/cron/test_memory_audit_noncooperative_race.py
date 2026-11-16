import hashlib
import os

import pytest

from cron.memory_audit_policy import RunPolicy
from tools.memory_tool_store import MemoryStore


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('foreign_target', ['none', 'memory', 'user'])
def test_uncooperative_rename_at_commit_preserves_both_stores(tmp_path, monkeypatch, foreign_target):
    from tools import memory_tool

    home = tmp_path / 'home'
    memories = home / 'memories'
    memories.mkdir(parents=True)
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    paths = {'memory': memories / 'MEMORY.md', 'user': memories / 'USER.md'}
    original = b'first\n\xc2\xa7\nsecond'
    external = b'first\n\xc2\xa7\nexternal update'
    for path in paths.values():
        path.write_bytes(original)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    store = MemoryStore()
    store._memory_audit_policy = RunPolicy('complete', hashes, None)
    import tools.memory_tool_store as memory_store
    replace = os.replace
    exchange = memory_store._rename_exchange_at
    injections = []

    def inject_before_audit_exchange(parent_fd, source_name, target_name):
        if foreign_target != 'none' and target_name == 'MEMORY.md' and not injections:
            other = memories / '.external-update'
            other.write_bytes(external)
            replace(other, paths[foreign_target])
            injections.append(foreign_target)
        return exchange(parent_fd, source_name, target_name)

    monkeypatch.setattr(memory_store, '_rename_exchange_at', inject_before_audit_exchange)
    result = store.add('memory', 'new audit entry')
    observed = {name: path.read_bytes() for name, path in paths.items()}
    if foreign_target == 'none':
        assert result.get('success') is True, result
        assert observed == {'memory': original + b'\n\xc2\xa7\nnew audit entry', 'user': original}
        assert not injections
    else:
        assert injections == [foreign_target]
        assert result.get('success') is False, {'result': result, 'observed': observed}
        assert observed[foreign_target] == external
        if foreign_target == 'user':
            assert observed['memory'] == original


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('foreign_target', ['memory', 'user'])
def test_uncooperative_rename_after_exchange_is_refused(tmp_path, monkeypatch, foreign_target):
    from tools import memory_tool
    import tools.memory_tool_store as memory_store

    home = tmp_path / 'home'
    memories = home / 'memories'
    memories.mkdir(parents=True)
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    paths = {'memory': memories / 'MEMORY.md', 'user': memories / 'USER.md'}
    original = b'first\n\xc2\xa7\nsecond'
    external = b'first\n\xc2\xa7\nexternal update'
    for path in paths.values():
        path.write_bytes(original)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    store = MemoryStore()
    store._memory_audit_policy = RunPolicy('complete', hashes, None)
    exchange = memory_store._rename_exchange_at
    injected = []

    def inject_after_exchange(parent_fd, source_name, target_name):
        result = exchange(parent_fd, source_name, target_name)
        if target_name == 'MEMORY.md' and not injected:
            other = memories / '.external-update'
            other.write_bytes(external)
            os.replace(other, paths[foreign_target])
            injected.append(foreign_target)
        return result

    monkeypatch.setattr(memory_store, '_rename_exchange_at', inject_after_exchange)
    result = store.add('memory', 'new audit entry')
    observed = {name: path.read_bytes() for name, path in paths.items()}
    assert injected == [foreign_target]
    assert result.get('success') is False, {'result': result, 'observed': observed}
    assert observed[foreign_target] == external
    if foreign_target == 'user':
        assert observed['memory'] == original


@pytest.mark.platforms('posix')
def test_post_commit_exception_restores_original_bytes(tmp_path, monkeypatch):
    from tools import memory_tool

    home = tmp_path / 'home'
    memories = home / 'memories'
    memories.mkdir(parents=True)
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    original = b'first\n\xc2\xa7\nsecond'
    (memories / 'MEMORY.md').write_bytes(original)
    (memories / 'USER.md').write_bytes(original)
    hashes = {name: hashlib.sha256((memories / filename).read_bytes()).hexdigest()
              for name, filename in (('memory', 'MEMORY.md'), ('user', 'USER.md'))}
    store = MemoryStore()
    policy = RunPolicy('complete', hashes, None)
    store._memory_audit_policy = policy

    def fail_after_exchange(*_args):
        raise RuntimeError('post-commit probe')

    monkeypatch.setattr(policy, 'matches_store_after_write', fail_after_exchange)
    with pytest.raises(RuntimeError, match='post-commit probe'):
        store.add('memory', 'new audit entry')
    assert (memories / 'MEMORY.md').read_bytes() == original
    assert (memories / 'USER.md').read_bytes() == original
    assert not list(memories.glob('.mem_*'))
