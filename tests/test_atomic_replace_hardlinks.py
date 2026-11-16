import errno
import os

import pytest

from utils import atomic_replace


@pytest.mark.parametrize('failure', [errno.EBUSY, errno.EXDEV])
@pytest.mark.parametrize('symlink', [False, True])
def test_in_place_fallback_refuses_shared_inode(tmp_path, monkeypatch, failure, symlink):
    protected = tmp_path / 'protected'
    protected.write_bytes(b'original')
    alias = tmp_path / 'alias'
    os.link(protected, alias)
    destination = alias
    if symlink:
        destination = tmp_path / 'link'
        destination.symlink_to(alias)
    staged = tmp_path / 'staged'
    staged.write_bytes(b'replacement')
    inode = protected.stat().st_ino
    attempts = []

    def fail_replace(source, target):
        attempts.append((source, target))
        raise OSError(failure, 'synthetic cross-device or bind-mount rename refusal')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='atomic_replace_hardlink_target'):
        atomic_replace(staged, destination)
    assert len(attempts) == 1
    assert protected.read_bytes() == alias.read_bytes() == b'original'
    assert protected.stat().st_ino == alias.stat().st_ino == inode
    assert staged.read_bytes() == b'replacement'
    assert destination.is_symlink() == symlink


@pytest.mark.parametrize('failure', [errno.EBUSY, errno.EXDEV])
def test_single_link_fallback_keeps_original_behavior(tmp_path, monkeypatch, failure):
    target = tmp_path / 'target'
    target.write_bytes(b'original')
    staged = tmp_path / 'staged'
    staged.write_bytes(b'replacement')
    inode = target.stat().st_ino

    def fail_replace(source, destination):
        raise OSError(failure, 'synthetic cross-device or bind-mount rename refusal')

    monkeypatch.setattr(os, 'replace', fail_replace)
    assert atomic_replace(staged, target) == str(target)
    assert target.read_bytes() == b'replacement'
    assert target.stat().st_ino == inode
    assert not staged.exists()


def test_hardlink_rename_replaces_only_its_directory_entry(tmp_path):
    protected = tmp_path / 'protected'
    protected.write_bytes(b'original')
    alias = tmp_path / 'alias'
    os.link(protected, alias)
    staged = tmp_path / 'staged'
    staged.write_bytes(b'replacement')
    assert atomic_replace(staged, alias) == str(alias)
    assert alias.read_bytes() == b'replacement'
    assert protected.read_bytes() == b'original'
    assert alias.stat().st_ino != protected.stat().st_ino
