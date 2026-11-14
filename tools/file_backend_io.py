"""Candidate: file-state I/O follows the file tool's selected environment.

This is core glue, not a provider hook: provider flags cannot intercept Python's
host os.stat/open calls. Context is thread/task scoped and never grants paths.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
import hashlib
import re
import sys

from tools.file_backend_probe import FileBackendUnavailable, environment_generation, probe_environment

_active = ContextVar('hermes_file_backend_io', default=None)
_generation: ContextVar[str | None] = ContextVar('hermes_file_backend_generation', default=None)
_hosted_compat = ContextVar('hermes_file_hosted_compat', default=False)


def revoke_backend_reads(env):
    from tools.file_tools_read_tracking import _read_tracker_lock
    with _read_tracker_lock:
        _revoke_backend_reads_locked(env)


def _revoke_backend_reads_locked(env):
    from tools.file_tools_read_tracking import _read_tracker
    from tools.file_protected_fuse import protected_fuse
    if protected_fuse(env) is not None:
        env._read_authorization_epoch = getattr(env, '_read_authorization_epoch', 0) + 1
    for data in _read_tracker.values():
        binding = data.get('backend_binding')
        if binding is None or binding[0] is not env:
            continue
        data['io_revision'] = data.get('io_revision', 0) + 1
        for key in ('dedup', 'dedup_hits', 'read_timestamps', 'read_coverage',
                    'full_write_baselines', 'storage_paths', 'protected_tickets',
                    'blind_patches', 'dedup_generation_reads', 'not_found'):
            data.get(key, {}).clear()
        data['backend_binding'] = (env, None)


def mutation_boundary(env):
    """Revoke all tasks and in-flight reads; keep only this caller's new epoch."""
    revoke_backend_reads(env)
    generation = checked_generation(env)
    if _active.get() is env:
        _generation.set(generation)
    from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock
    with _read_tracker_lock:
        for data in _read_tracker.values():
            binding = data.get('backend_binding')
            if binding is not None and binding[0] is env:
                data['backend_binding'] = (env, generation)
    return generation


def checked_generation(env, expected=None):
    generation = environment_generation(env)
    if expected is not None and generation != expected:
        revoke_backend_reads(env)
        raise FileBackendUnavailable('backend generation changed during file operation; refusing write')
    return generation


def _check_generation(env):
    return checked_generation(env, _generation.get())


def checked_file_ops(ops):
    from tools.environments.sandbox_scope import ScopedEnvironment
    bound = _active.get()
    if isinstance(bound, ScopedEnvironment) and getattr(ops, 'env', None) is not bound:
        revoke_backend_reads(bound)
        raise FileBackendUnavailable('hosted file backend changed during operation; retry')
    return ops


def storage_path(path):
    if isinstance(path, str) and path.startswith('hermes-storage://'):
        return path
    if _hosted_compat.get():
        from tools.environments.sandbox_scope import current_sandbox_key
        return 'hermes-storage://hosted-compat/' + current_sandbox_key() + '/' + path
    env = _active.get()
    if env is None:
        return _host_storage_path(path)
    _check_generation(env)
    result = probe_environment(env, path)
    _check_generation(env)
    if result.state == 'unavailable':
        revoke_backend_reads(env)
        raise FileBackendUnavailable(result.error + '; refusing write')
    return 'hermes-storage://' + result.data['storage_id'] + result.data['storage_path']


def _host_storage_path(path):
    canonical = os.path.realpath(path)
    if sys.platform != 'linux':
        return canonical
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        mounts = [line.split() for line in stream]
    mounts = [m for m in mounts if canonical == unescape(m[4]) or canonical.startswith(unescape(m[4]).rstrip('/') + '/')]
    mount = max(mounts, key=lambda m: len(unescape(m[4])))
    separator = mount.index('-')
    fstype, source = mount[separator + 1:separator + 3]
    with open('/proc/sys/kernel/random/boot_id', encoding='utf-8') as stream:
        host = stream.read().strip()
    identity = [fstype, source] if fstype in ('nfs', 'nfs4', 'cifs') else [host, mount[2], fstype, source]
    storage_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    canonical = os.path.normpath(unescape(mount[3]).rstrip('/') + '/' + os.path.relpath(canonical, unescape(mount[4])))
    return 'hermes-storage://' + storage_id + canonical


@contextmanager
def backend_io_scope(task_id):
    from tools.file_tools import _get_file_ops
    from tools.environments.base import env_uses_host_filesystem
    ops = _get_file_ops(task_id or 'default')
    env = getattr(ops, 'env', None)
    from tools.file_operations import ShellFileOperations
    if env is None and isinstance(ops, ShellFileOperations):
        raise FileBackendUnavailable('file backend has no execution identity; refusing host fallback')
    backend = None if env is None or env_uses_host_filesystem(env) else env
    from tools.environments.sandbox_scope import ScopedEnvironment
    if isinstance(backend, ScopedEnvironment) and (
        not callable(getattr(backend, 'file_generation', None))
        or not callable(getattr(backend, 'execute_file_probe', None))
    ):
        revoke_backend_reads(backend)
        if backend._file_authority_capable:
            raise FileBackendUnavailable('hosted backend file authority disappeared; refusing operation')
        from tools.file_tools_read_tracking import _read_tracker_lock, _task_data
        with _read_tracker_lock:
            data = _task_data(task_id or 'default')
            if data.get('backend_binding') != (backend, None):
                data['io_revision'] = data.get('io_revision', 0) + 1
                for key in ('dedup', 'dedup_hits', 'read_timestamps', 'read_coverage',
                            'full_write_baselines', 'storage_paths', 'protected_tickets',
                            'blind_patches', 'dedup_generation_reads', 'not_found'):
                    data.get(key, {}).clear()
                data['backend_binding'] = (backend, None)
        token = _active.set(backend)
        generation_token = _generation.set(None)
        compat_token = _hosted_compat.set(True)
        try:
            yield
        finally:
            _hosted_compat.reset(compat_token)
            _generation.reset(generation_token)
            _active.reset(token)
        return
    if isinstance(backend, ScopedEnvironment):
        object.__setattr__(backend, '_file_authority_capable', True)
    try:
        generation = 'host' if backend is None else checked_generation(backend)
    except FileBackendUnavailable:
        from tools.file_tools_read_tracking import _read_tracker_lock, _task_data
        with _read_tracker_lock:
            data = _task_data(task_id or 'default')
            data['io_revision'] = data.get('io_revision', 0) + 1
            for key in ('dedup', 'read_coverage', 'full_write_baselines', 'protected_tickets'):
                data.get(key, {}).clear()
            data['backend_binding'] = (backend, None)
        raise
    from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock, _task_data
    with _read_tracker_lock:
        data = _task_data(task_id or 'default')
        bindings = (entry.get('backend_binding') for entry in _read_tracker.values())
        generation_changed = backend is not None and any(
            old and old[0] is backend and old[1] not in (None, generation) for old in bindings)
        binding = backend, generation
        if data.get('backend_binding', binding) != binding:
            data['io_revision'] = data.get('io_revision', 0) + 1
            for key in ('dedup', 'dedup_hits', 'read_timestamps', 'read_coverage',
                        'full_write_baselines', 'storage_paths', 'protected_tickets', 'blind_patches', 'dedup_generation_reads', 'not_found'):
                data.get(key, {}).clear()
        data['backend_binding'] = binding
        if generation_changed:
            _revoke_backend_reads_locked(backend)
            raise FileBackendUnavailable('backend generation changed before file operation; re-read the file')
    token = _active.set(backend)
    generation_token = _generation.set(generation if backend is not None else None)
    try:
        yield
    except FileBackendUnavailable:
        if backend is not None:
            revoke_backend_reads(backend)
        raise
    finally:
        _generation.reset(generation_token)
        _active.reset(token)


@contextmanager
def saved_backend_scope(binding):
    if binding is None:
        raise FileBackendUnavailable('saved file backend identity is unavailable')
    backend, generation = binding
    if backend is not None:
        checked_generation(backend, generation)
    token = _active.set(backend)
    generation_token = _generation.set(generation if backend is not None else None)
    try:
        yield
    finally:
        _generation.reset(generation_token)
        _active.reset(token)


def _search_preflight_error(path):
    from agent.file_safety import get_nt_namespace_error, get_read_block_error
    if not isinstance(path, str):
        return None
    error = get_nt_namespace_error(path, verb='Search')
    if not error and os.path.isabs(path):
        error = get_read_block_error(path)
    return error


def file_backend_call(fn):
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        task_id = bound.arguments.get('task_id', 'default')
        header_paths: list[str] = []
        if fn.__name__ == 'search_tool':
            error = _search_preflight_error(bound.arguments['path'])
            if error:
                return json.dumps({'error': error})
        if fn.__name__ == 'write_file_tool':
            from tools.file_tools_write_guards import _check_sensitive_path, _is_internal_file_tool_content
            path = bound.arguments['path']
            error = _check_sensitive_path(path, task_id) if isinstance(path, str) and os.path.isabs(path) else None
            if not error and _is_internal_file_tool_content(bound.arguments['content']):
                from tools.file_tools_write_guards import _INTERNAL_DISPLAY_CONTENT_ERROR as error
            if error:
                return json.dumps({'error': error})
        if fn.__name__ == 'patch_tool' and bound.arguments.get('mode') == 'patch' and bound.arguments.get('patch'):
            from tools.file_tools import _collect_v4a_header_paths, _write_precheck_error
            collected = _collect_v4a_header_paths(bound.arguments['patch'])
            if isinstance(collected, str):
                return collected
            header_paths = collected[0]
            error = _write_precheck_error(
                ([bound.arguments['path']] if bound.arguments.get('path') else []) + header_paths,
                collected[1], task_id, bound.arguments.get('cross_profile', False))
            if error:
                from tools.registry import tool_error
                return tool_error(error)
        if fn.__name__ in ('write_file_tool', 'patch_tool', 'search_tool'):
            from tools.environments.sandbox_scope import hosted_scope
            if hosted_scope() is not None:
                from tools.file_tools_paths import _hosted_tilde
                try:
                    for path in [bound.arguments.get('path'), *header_paths]:
                        if isinstance(path, str) and path.startswith('~'):
                            _hosted_tilde(path, task_id)
                except ValueError as exc:
                    return json.dumps({'error': str(exc)})
        if fn.__name__ == 'read_file_tool':
            from tools.file_tools import _is_blocked_device, _read_device_base, get_nt_namespace_error
            from tools.file_tools_paths import _hosted_tilde
            path = bound.arguments['path']
            if error := get_nt_namespace_error(path, verb='Read'):
                return json.dumps({'error': error})
            try:
                checked_path = _hosted_tilde(path, task_id)
                base, _ = _read_device_base(checked_path, task_id)
            except (OSError, ValueError) as exc:
                return json.dumps({'error': str(exc)})
            if _is_blocked_device(checked_path, base_dir=base):
                return json.dumps({'error': f"Cannot read '{path}': this is a device file that would block or produce infinite output."})
        try:
            with backend_io_scope(task_id):
                return fn(*args, **kwargs)
        except (OSError, RuntimeError) as exc:
            from tools.registry import tool_error
            return tool_error(str(exc))
    return wrapped


def remote_probe(path, *, hash_bytes=False):
    env = _active.get()
    if env is None:
        raise FileBackendUnavailable('remote file-state probe without backend context')
    _check_generation(env)
    result = probe_environment(env, path, hash_bytes=hash_bytes)
    _check_generation(env)
    if result.state == 'absent':
        raise FileNotFoundError(result.data['errno'], 'backend file does not exist', str(path))
    if result.state != 'present':
        revoke_backend_reads(env)
        raise FileBackendUnavailable(result.error + '; refusing write')
    return result.data


def file_exists(path):
    if _active.get() is None:
        try:
            os.stat(path)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False
    try:
        remote_probe(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def file_mtime(path):
    if _active.get() is None:
        return os.path.getmtime(path)
    return remote_probe(path)['mtime']


def file_metadata(path):
    if _active.get() is None:
        s = os.stat(path)
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    return tuple(remote_probe(path)['metadata'])


def remote_file_version(path):
    if _active.get() is None:
        return False, None
    try:
        data = remote_probe(path, hash_bytes=True)
        return True, (*data['metadata'], bytes.fromhex(data['sha256']))
    except (FileNotFoundError, NotADirectoryError):
        return True, None
