"""Carry write_file authorization through to its native/backend commit boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
import json
import shlex

from tools import file_operations_commit as commit
from tools.file_backend_io import _active, _check_generation, checked_generation, revoke_backend_reads, storage_path
from tools.file_backend_probe import FileBackendUnavailable, probe_environment
from tools.file_operations_common import ExecuteResult

_binding = ContextVar('hermes_file_write_binding', default=None)


@dataclass(frozen=True)
class WriteBinding:
    path: str
    storage_key: str
    target: dict
    env: object
    generation: str | None
    interpreter: tuple | None
    protected_ticket: object = None


def capture_write_binding(path, task_id=None):
    env = _active.get()
    if env is None:
        target = commit.target_snapshot(path)
        return WriteBinding(path, storage_path(path), target, None, None, None)
    generation = _check_generation(env)
    from tools.file_protected_fuse import protected_fuse
    if protected_fuse(env) is not None:
        from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock
        with _read_tracker_lock:
            ticket = (_read_tracker.get(task_id) or {}).get('protected_tickets', {}).get(path)
        if ticket is None:
            from dataclasses import replace
            target = env.target(path)
            if target.absent:
                ticket = replace(target, task_id=task_id)
        if ticket is None or ticket.task_id != task_id or ticket.generation != generation:
            raise FileBackendUnavailable('protected FUSE requires this task full unredacted read; refusing write')
        target = {'version': None if ticket.absent else list(ticket.metadata), 'sha256': ticket.sha256}
        return WriteBinding(path, ticket.storage_key, target, env, generation, None, ticket)
    result = probe_environment(env, path, hash_bytes=True, commit=True)
    _check_generation(env)
    if result.state not in ('present', 'absent') or 'target' not in result.data:
        raise FileBackendUnavailable('backend commit target could not be bound; refusing write')
    data = result.data
    return WriteBinding(path, 'hermes-storage://' + data['storage_id'] + data['storage_path'],
                        data['target'], env, generation, (data['python'], data['identity']))


@contextmanager
def bind_write_commit(binding):
    """Thread/task-local; never mutable state on the shared FileOperations adapter."""
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


def authorized_write(binding, task_id):
    """The snapshot passed to commit must be the full version the task actually saw."""
    from tools import file_state
    from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock
    if binding.protected_ticket is not None and not binding.protected_ticket.absent:
        with _read_tracker_lock:
            saved = (_read_tracker.get(task_id) or {}).get('protected_tickets', {}).get(binding.path)
        if saved != binding.protected_ticket or saved.task_id != task_id:
            raise FileBackendUnavailable('protected FUSE task read authorization changed; refusing write')
    if (file_state.guard_disabled() and binding.protected_ticket is None) or binding.target['version'] is None:
        return
    with _read_tracker_lock:
        data = _read_tracker.get(task_id) or {}
        baseline = data.get('full_write_baselines', {}).get(binding.path)
        key = data.get('storage_paths', {}).get(binding.path)
    version = (*binding.target['version'], bytes.fromhex(binding.target['sha256']))
    if baseline != version or key != binding.storage_key:
        raise FileBackendUnavailable('write authorization changed before commit; re-read the file; refusing write')


def bound_atomic_write(operations, path, content):
    binding = _binding.get()
    if binding is None:
        from tools.file_protected_patch import protected_patch
        state = protected_patch(operations)
        if state is not None:
            return state.publish(path, content)
        from tools.file_protected_fuse import protected_fuse
        if protected_fuse(operations.env) is not None:
            raise FileBackendUnavailable('protected FUSE requires a bound native write; unsupported operation, no replace fallback')
        return None
    if binding.path != path:
        raise FileBackendUnavailable('write path differs from authorized commit target; refusing write')
    data = content.encode('utf-8', 'surrogateescape')
    if binding.env is None:
        from tools.environments.base import env_uses_host_filesystem
        if not env_uses_host_filesystem(operations.env):
            raise FileBackendUnavailable('host commit binding used by a non-host backend; refusing write')
        error = commit.commit_write(path, data, binding.target)
        return ExecuteResult(stdout=error or '', exit_code=1 if error else 0)
    env = binding.env
    if operations.env is not env:
        revoke_backend_reads(env)
        raise FileBackendUnavailable('backend generation changed before commit; refusing write')
    checked_generation(env, binding.generation)
    if binding.protected_ticket is not None:
        return env.publish(binding.protected_ticket, content)
    # Validate the same interpreter *inside* the committing process, before it
    # consumes stdin or mutates storage. No persisted shell snapshot/PATH lookup.
    program = inspect.getsource(commit) + '''
import json,sys
expected=json.loads(sys.argv[2])
python=os.path.realpath(sys.executable)
s=os.stat(python)
with open(python,'rb') as f: digest=hashlib.sha256(f.read()).hexdigest()
identity=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,digest]
if [python,identity]!=expected:
 raise RuntimeError('backend commit interpreter changed; refusing write')
error=commit_write(sys.argv[1],sys.stdin.buffer.read(),json.loads(sys.argv[3]))
if error: print(error)
raise SystemExit(1 if error else 0)
'''
    command = 'exec ' + shlex.quote(binding.interpreter[0]) + ' -I -B -c ' + shlex.quote(program)
    command += ' ' + ' '.join(shlex.quote(arg) for arg in (path, json.dumps(binding.interpreter), json.dumps(binding.target)))
    from tools.file_backend_probe import execute_commit
    result = execute_commit(env, command, content)
    try:
        checked_generation(env, binding.generation)
    except FileBackendUnavailable as exc:
        raise FileBackendUnavailable('backend generation changed during commit; write outcome unknown') from exc
    return ExecuteResult(stdout=result.get('output', ''), exit_code=result.get('returncode', 1))
