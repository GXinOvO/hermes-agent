"""Native host qualification of the bound write_file commit and publication."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tools import file_operations_commit as commit
from tools import file_tools as ft
from tools import terminal_tool as tt
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


@pytest.fixture
def host_ops(tmp_path, monkeypatch):
    env = LocalEnvironment(cwd=str(tmp_path))
    task = 'commit-native-host'
    monkeypatch.delenv('HERMES_DISABLE_FILE_STATE_GUARD', raising=False)
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    key = tt._resolve_container_task_id(task)
    monkeypatch.setitem(tt._active_environments, key, env)
    ops = ShellFileOperations(env)
    monkeypatch.setitem(ft._file_ops_cache, key, ops)
    try:
        yield ops, task
    finally:
        ft.clear_file_ops_cache(task)
        env.cleanup()


@pytest.mark.parametrize('native', ['1', '0'])
def test_host_tool_refuses_version_change_at_commit(host_ops, tmp_path, monkeypatch, native):
    ops, task = host_ops
    monkeypatch.setenv('HERMES_NATIVE_FILE_READ', native)
    target = tmp_path / 'known.txt'
    target.write_text('known original\n')
    assert 'known original' in json.loads(ft.read_file_tool(str(target), task_id=task))['content']
    original = ops._atomic_write
    injected = []

    def mutate(path, content):
        target.write_text('external preserved\n')
        injected.append(path)
        return original(path, content)

    monkeypatch.setattr(ops, '_atomic_write', mutate)
    result = json.loads(ft.write_file_tool(str(target), 'must refuse\n', task_id=task))
    assert injected == [str(target)]
    assert result.get('error') and not result.get('verified'), result
    assert target.read_text() == 'external preserved\n'


def test_new_target_cannot_be_occupied_after_final_check(host_ops, tmp_path, monkeypatch):
    _ops, task = host_ops
    target = tmp_path / 'new.txt'
    original = os.link
    injected = []

    def occupy(src, dst, **kwargs):
        target.write_text('late foreign file\n')
        injected.append(dst)
        return original(src, dst, **kwargs)

    monkeypatch.setattr(commit.os, 'link', occupy)
    result = json.loads(ft.write_file_tool(str(target), 'must refuse\n', task_id=task))
    assert injected == ['new.txt']
    assert result.get('error') and not result.get('verified'), result
    assert target.read_text() == 'late foreign file\n'
    assert not list(tmp_path.glob('.hermes-tmp.*'))


def test_two_real_threads_publish_absent_target_without_clobber(tmp_path, monkeypatch):
    target = tmp_path / 'contended.txt'
    snapshot = commit.target_snapshot(str(target))
    rendezvous = Barrier(2)
    original = commit.os.link

    def simultaneous(*args, **kwargs):
        rendezvous.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(commit.os, 'link', simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(commit.commit_write, str(target), data, snapshot) for data in (b'one\n', b'two\n')]
        results = [future.result(timeout=10) for future in futures]
    assert sum(result is None for result in results) == 1, results
    assert sum(result is not None and 'File exists' in result for result in results) == 1, results
    assert target.read_bytes() in (b'one\n', b'two\n')
    assert not list(tmp_path.glob('.hermes-tmp.*'))
