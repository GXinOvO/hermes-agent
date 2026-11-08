"""Commit-boundary regressions for FBO-R3-001, using real file tool writes."""
import json

import pytest

from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_tools as ft

remote_project = backend_fixtures.remote_project


@pytest.mark.parametrize('change', ['same-inode', 'new-inode', 'new-file', 'ancestor', 'generation'])
def test_commit_refuses_changed_authorized_target(remote_project, monkeypatch, change):
    project, env, task = remote_project
    parent = project / 'directory'
    parent.mkdir()
    target = parent / 'target.txt'
    if change != 'new-file':
        target.write_text('fully read original\n')
        assert 'fully read original' in json.loads(ft.read_file_tool('/workspace/directory/target.txt', task_id=task))['content']
    other = project / 'other'
    other.mkdir()
    (other / 'target.txt').write_text('unread ancestor target\n')
    ops = ft._get_file_ops(task)
    original = ops._atomic_write
    injected = []

    def replace_inode():
        target.unlink()
        target.write_text('unexpected replacement\n')

    def replace_ancestor():
        parent.rename(project / 'original-directory')
        parent.symlink_to('other')

    def replace_generation():
        env._session_id += '-changed-before-commit'

    mutations = {'same-inode': lambda: target.write_text('external changed content\n'),
                 'new-inode': replace_inode, 'new-file': lambda: target.write_text('unexpected new file\n'),
                 'ancestor': replace_ancestor, 'generation': replace_generation}

    def mutate(path, content):
        mutations[change]()
        injected.append(change)
        return original(path, content)

    monkeypatch.setattr(ops, '_atomic_write', mutate)
    result = json.loads(ft.write_file_tool('/workspace/directory/target.txt', 'forbidden write\n', task_id=task))
    assert injected == [change]
    assert result.get('error') and not result.get('verified'), result
    expected = {'same-inode': 'external changed content\n', 'new-inode': 'unexpected replacement\n',
                'new-file': 'unexpected new file\n', 'ancestor': 'unread ancestor target\n',
                'generation': 'fully read original\n'}[change]
    assert target.read_text() == expected
    assert not list(project.rglob('.hermes-tmp.*'))
    if change == 'ancestor':
        assert (project / 'original-directory/target.txt').read_text() == 'fully read original\n'


def test_new_nested_file_and_static_ancestor_alias_remain_usable(remote_project):
    project, _env, task = remote_project
    directory = project / 'real'
    directory.mkdir()
    (project / 'alias').symlink_to('real', target_is_directory=True)
    path = '/workspace/alias/nested/new.txt'
    first = json.loads(ft.write_file_tool(path, 'legitimate first\n', task_id=task))
    assert first.get('verified'), first
    assert (directory / 'nested/new.txt').read_text() == 'legitimate first\n'
    second = json.loads(ft.write_file_tool(path, 'legitimate second\n', task_id=task))
    assert second.get('verified'), second
    assert (project / 'alias').is_symlink()
    assert (directory / 'nested/new.txt').read_text() == 'legitimate second\n'


def test_replaced_alias_to_same_target_is_refused(remote_project, monkeypatch):
    project, env, task = remote_project
    (project / 'known.txt').write_text('known original\n')
    (project / 'alias.txt').symlink_to('known.txt')
    assert 'known original' in json.loads(ft.read_file_tool('/workspace/alias.txt', task_id=task))['content']
    ops = ft._get_file_ops(task)
    original = ops._atomic_write
    injected = []

    def replace_alias(path, content):
        changed = env.execute('ln -s known.txt /workspace/new-alias; mv -Tf /workspace/new-alias /workspace/alias.txt')
        assert changed['returncode'] == 0, changed
        injected.append(path)
        return original(path, content)

    monkeypatch.setattr(ops, '_atomic_write', replace_alias)
    result = json.loads(ft.write_file_tool('/workspace/alias.txt', 'must refuse\n', task_id=task))
    assert injected == ['/workspace/alias.txt']
    assert result.get('error') and not result.get('verified'), result
    assert (project / 'known.txt').read_text() == 'known original\n'
