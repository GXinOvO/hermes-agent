from contextlib import ExitStack
import errno
import json
import os

import pytest

from cron.memory_audit_policy import RunPolicy
from hermes_cli import backup, backup_cron_prompts, post_update
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.fixture
def updater_restore(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'memories').mkdir(parents=True)
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: home / 'memories')
    for name in ('MEMORY.md', 'USER.md'):
        (home / 'memories' / name).write_bytes(b'synthetic memory')

    def prepare(caller, target=None, alias_kind='symlink'):
        if caller == 'cron_prompt':
            live = json.dumps({'jobs': [{'id': 'synthetic-job', 'name': 'brief', 'prompt': 'brief'}]}).encode()
            source = json.dumps({'jobs': [{'id': 'synthetic-job', 'name': 'brief', 'prompt': 'synthetic restored prompt'}]}).encode()
            alias = home / 'cron' / 'jobs.json'
            snapshot = backup._quick_snapshot_root(home) / 'fixture' / 'cron' / 'jobs.json'
        else:
            live = b'model:\n  provider: synthetic-live\n  default: live-model\nnotes: preserved\n'
            source = b'model:\n  provider: synthetic-snapshot\n  default: snapshot-model\nnotes: preserved\n'
            alias = home / 'config.yaml'
            snapshot = backup._quick_snapshot_root(home) / 'fixture' / 'config.yaml'
        destination = home / target if target else alias
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(live)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(source)
        if target:
            if alias_kind == 'hardlink':
                os.link(destination, alias)
            else:
                alias.symlink_to(destination)

        def restore():
            if caller == 'cron_prompt':
                return backup_cron_prompts.restore_cron_prompt_fields_if_degraded('fixture', home)
            if caller == 'config_model':
                return backup.restore_config_model_settings_if_rewritten('fixture', home)
            if caller == 'docker_rollback':
                from scripts import docker_config_migrate
                return docker_config_migrate._restore_backups({alias: snapshot})
            assert caller == 'rollback'
            return post_update._restore_backups({alias: snapshot})

        return alias, destination, live, restore

    return home, prepare


@pytest.mark.parametrize('caller', ['cron_prompt', 'config_model', 'rollback', 'docker_rollback'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['policy', 'locks'])
def test_updater_restore_refuses_memory_symlink(updater_restore, caller, target, protection, caplog):
    home, prepare = updater_restore
    alias, destination, original, restore = prepare(caller, target)
    inode = destination.stat().st_ino
    with ExitStack() as stack:
        if protection == 'policy':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        error = None
        try:
            result = restore()
        except OSError as exc:
            result, error = None, str(exc)
        assert destination.read_bytes() == original
        assert destination.stat().st_ino == inode
        assert alias.is_symlink()
        expected = 'memory_audit_admin_lock_file' if target.endswith('.lock') else 'memory_audit_admin_alias'
        assert expected in (error or caplog.text)
        assert not result


@pytest.mark.parametrize('caller', ['cron_prompt', 'config_model', 'rollback', 'docker_rollback'])
@pytest.mark.parametrize('alias_kind', ['regular', 'symlink'])
def test_updater_restore_preserves_normal_targets(updater_restore, caller, alias_kind):
    _, prepare = updater_restore
    target = 'normal-target.txt' if alias_kind == 'symlink' else None
    alias, destination, _, restore = prepare(caller, target)
    result = restore()
    assert result
    if caller == 'cron_prompt':
        assert json.loads(destination.read_text())['jobs'][0]['prompt'] == 'synthetic restored prompt'
    else:
        assert 'snapshot-model' in destination.read_text()
        assert 'notes: preserved' in destination.read_text()
    assert alias.is_symlink() == (alias_kind == 'symlink')


@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
def test_updater_rollback_refuses_shared_inode(updater_restore, target):
    _, prepare = updater_restore
    _, destination, original, restore = prepare('rollback', target, 'hardlink')
    inode = destination.stat().st_ino
    with pytest.raises(OSError):
        restore()
    assert destination.read_bytes() == original
    assert destination.stat().st_ino == inode


@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
def test_migration_cannot_normalize_env_through_memory_alias(updater_restore, target):
    home, _ = updater_restore
    destination = home / 'memories' / target
    original = b'  SYNTHETIC_SETTING=value  \n'
    destination.write_bytes(original)
    (home / '.env').symlink_to(destination)
    (home / 'config.yaml').write_text('_config_version: 20\nplugins:\n  disabled: [[x]]\n')
    try:
        result = post_update.step_migrate_config()
    except OSError as exc:
        result = str(exc)
    assert destination.read_bytes() == original
    assert result == 'memory_audit_admin_alias'


@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
@pytest.mark.parametrize('entry', ['post_update', 'direct', 'docker'])
def test_migration_config_alias_cannot_publish(updater_restore, target, entry, caplog, capsys):
    from hermes_cli import config

    home, _ = updater_restore
    destination = home / 'memories' / target
    original = b'_config_version: 20\nplugins:\n  disabled: []\n'
    destination.write_bytes(original)
    (home / 'config.yaml').symlink_to(destination)
    inode = destination.stat().st_ino
    result, error = None, None
    try:
        if entry == 'post_update':
            result = post_update.step_migrate_config()
        elif entry == 'direct':
            result = config.migrate_config(interactive=False, quiet=True)
        else:
            from scripts import docker_config_migrate
            result = docker_config_migrate.main()
    except OSError as exc:
        error = str(exc)
    assert destination.read_bytes() == original
    assert destination.stat().st_ino == inode
    if entry == 'post_update':
        assert error == 'memory_audit_admin_alias'
    elif entry == 'direct':
        assert error is None and isinstance(result, dict)
        assert result['failed_step'] == 21
        assert 'memory_audit_admin_alias' in result['failed_warning']
    else:
        assert error is None and result == 0
        assert 'migration stopped at v21' in capsys.readouterr().err
        assert 'memory_audit_admin_alias' in caplog.text


@pytest.mark.parametrize('target', ['MEMORY.md', 'USER.md'])
def test_direct_env_normalization_refuses_memory_alias(updater_restore, target):
    from hermes_cli.config import sanitize_env_file

    home, _ = updater_restore
    destination = home / 'memories' / target
    original = b'  SYNTHETIC_SETTING=value  \n'
    destination.write_bytes(original)
    (home / '.env').symlink_to(destination)
    with pytest.raises(OSError, match='memory_audit_admin_alias'):
        sanitize_env_file()
    assert destination.read_bytes() == original


@pytest.mark.parametrize('caller', ['cron_prompt', 'config_model'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['policy', 'locks'])
def test_updater_restore_refuses_hardlink_ebusy_fallback(
    updater_restore, caller, target, protection, monkeypatch
):
    home, prepare = updater_restore
    alias, destination, original, restore = prepare(caller, target, 'hardlink')
    inode = destination.stat().st_ino
    replace_attempts = []
    real_replace = os.replace

    def fail_bind_mount_replace(source, dest, *args, **kwargs):
        if os.path.abspath(os.fspath(dest)) == os.path.abspath(alias):
            replace_attempts.append(os.fspath(dest))
            raise OSError(errno.EBUSY, 'synthetic bind-mount rename refusal')
        return real_replace(source, dest, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', fail_bind_mount_replace)
    with ExitStack() as stack:
        if protection == 'policy':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        result = restore()
    assert destination.read_bytes() == original
    assert destination.stat().st_ino == inode == alias.stat().st_ino
    assert not result
    assert replace_attempts == [os.fspath(alias)]


@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
def test_config_writer_replaces_hardlink_without_modifying_memory(updater_restore, target):
    from hermes_cli.config_backend import Changes, FileBackend

    home, _ = updater_restore
    protected = home / target
    protected.write_bytes(b'model:\n  default: old-model\n')
    alias = home / 'config.yaml'
    os.link(protected, alias)
    original = protected.read_bytes()
    FileBackend.write_path(alias, Changes(set={'model.default': 'synthetic-model'}))
    assert protected.read_bytes() == original
    assert alias.stat().st_ino != protected.stat().st_ino


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('caller', ['post_update', 'docker'])
@pytest.mark.parametrize(('name', 'mode'), [('config.yaml', 0o644), ('.env', 0o640)])
@pytest.mark.parametrize('symlink', [False, True])
def test_updater_rollback_preserves_original_mode(tmp_path, caller, name, mode, symlink):
    home = tmp_path / caller
    home.mkdir()
    original = home / name
    target = home / 'linked-target' if symlink else original
    target.write_bytes(b'partially migrated state')
    target.chmod(mode)
    if symlink:
        original.symlink_to(target)
    backup = tmp_path / f'{caller}-{name.lstrip(".")}.backup'
    backup.write_bytes(b'original state')
    backup.chmod(mode)

    if caller == 'post_update':
        restored = post_update._restore_backups({original: backup})
    else:
        from scripts import docker_config_migrate
        restored = docker_config_migrate._restore_backups({original: backup})

    assert restored == [original]
    assert target.read_bytes() == b'original state'
    assert target.stat().st_mode & 0o7777 == mode
    assert original.is_symlink() == symlink


@pytest.mark.parametrize('entry', ['normalize', 'post_update', 'direct', 'docker'])
@pytest.mark.parametrize('target', ['memories/MEMORY.md', 'memories/USER.md', 'memories/MEMORY.md.lock',
                                    'memories/USER.md.lock', 'cron/.memory-audit.lock.lock'])
@pytest.mark.parametrize('protection', ['policy', 'locks'])
def test_env_migration_hardlink_ebusy_keeps_protected_inode(
    updater_restore, entry, target, protection, monkeypatch
):
    from hermes_cli import config

    home, _ = updater_restore
    destination = home / target
    before = b'  SYNTHETIC_SETTING=value  \n'
    destination.write_bytes(before)
    inode = destination.stat().st_ino
    alias = home / '.env'
    os.link(destination, alias)
    (home / 'config.yaml').write_text('_config_version: 20\nplugins:\n  disabled: []\n')
    real_replace = os.replace
    attempts = []

    def fail_bind_mount_replace(source, dest, *args, **kwargs):
        if os.path.abspath(os.fspath(dest)) == os.path.abspath(alias):
            attempts.append(os.fspath(dest))
            raise OSError(errno.EBUSY, 'synthetic bind-mount rename refusal')
        return real_replace(source, dest, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', fail_bind_mount_replace)
    with ExitStack() as stack:
        if protection == 'policy':
            token = memory_tool.bind_memory_audit_policy(RunPolicy('partial', {}, None))
            stack.callback(memory_tool.reset_memory_audit_policy, token)
        else:
            stack.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock'))
            for name in ('MEMORY.md', 'USER.md'):
                stack.enter_context(MemoryStore._file_lock(home / 'memories' / name))
        if entry == 'normalize':
            with pytest.raises(OSError, match='atomic_replace_hardlink_target'):
                config.sanitize_env_file()
        elif entry == 'post_update':
            assert post_update.step_migrate_config()['ok'] is True
        elif entry == 'direct':
            assert 'failed_step' not in config.migrate_config(interactive=False, quiet=True)
        else:
            from scripts import docker_config_migrate
            assert docker_config_migrate.main() == 0
    assert destination.read_bytes() == before
    assert destination.stat().st_ino == inode == alias.stat().st_ino
    assert attempts == [os.fspath(alias)]


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['post_update', 'docker'])
def test_failed_migration_restores_bytes_and_modes(updater_restore, entry, monkeypatch):
    import hermes_constants
    from hermes_cli import config

    home, _ = updater_restore
    monkeypatch.setattr(hermes_constants, '_detect_container', lambda: entry == 'docker')
    for key in ('HERMES_MANAGED', 'HERMES_SKIP_CHMOD', 'HERMES_CONTAINER'):
        monkeypatch.delenv(key, raising=False)
    if entry == 'docker':
        monkeypatch.setenv('HERMES_CONTAINER', '1')
    cp, ep = home / 'config.yaml', home / '.env'
    config_bytes = b'_config_version: 20\nplugins:\n  disabled: []\n'
    env_bytes = b'SYNTHETIC_VALUE=test\n'
    cp.write_bytes(config_bytes)
    ep.write_bytes(env_bytes)
    cp.chmod(0o644)
    ep.chmod(0o640)

    def fail_migration(**kwargs):
        cp.write_bytes(b'_config_version: 20\nchanged: true\n')
        ep.write_bytes(b'SYNTHETIC_VALUE=changed\n')
        raise ValueError('synthetic migration failure')

    if entry == 'post_update':
        monkeypatch.setattr(config, 'migrate_config', fail_migration)
        migrate = post_update.step_migrate_config
    else:
        from scripts import docker_config_migrate
        monkeypatch.setattr(docker_config_migrate, 'migrate_config', fail_migration)
        migrate = docker_config_migrate.main
    with pytest.raises(ValueError, match='synthetic migration failure'):
        migrate()
    assert cp.read_bytes() == config_bytes and ep.read_bytes() == env_bytes
    expected_config_mode = 0o644 if entry == 'docker' else 0o600
    expected_env_mode = 0o640 if entry == 'docker' else 0o600
    assert cp.stat().st_mode & 0o7777 == expected_config_mode
    assert ep.stat().st_mode & 0o7777 == expected_env_mode
