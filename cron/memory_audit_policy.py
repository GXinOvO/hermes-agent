"""Isolated experiment: bind memory-audit collection state to one Cron run."""

import base64
import errno
import hashlib
import json
import os
import re
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_cli._startup_fast import project_root_str
from hermes_constants import get_process_hermes_home
from cron.memory_audit_resources import PUBLISHER_RESOURCE_SHA256

JOB_ID = "562b2c8515ee"
SCRIPT_NAME = "builtin-memory-audit-input.py"
BASELINE_RELATIVE = 'reports/builtin-memory-audit-trial-20261110/baseline.json'
BASELINE_SHA256 = 'fb1b4968aaf4d73b5c5fbf20216a50f9041e965e78d48476a78ca822596a54f8'
PROFILE_HOME: Path | None = None
REPORT_ROOT = Path('/local_data/huguangyao/workspace/weekly-reports')
COLLECTOR_SHA256 = "ca1eed05c2958f7753d2aaf717c6665cd281908585e96c27f6bff0e9447c6576"
PUBLISHER_SHA256 = "b688300c15e81daa781004d9642536c33ca47637e2b5d36a3c68ea8e47ed357d"
PROMPT_SHA256 = "aa0e40d55cfc0833d2e9b79d56c37c2510b77a81c134064c1cc4d59a789f6b5b"
UNSHARE_SHA256 = "ea175949d95fb64dcf6131758c6fc78ca318a241f7d4cb264ed63fbb5de77bcf"
PUBLISHER_DEPENDENCY_SHA256 = {
    "/local_data/huguangyao/.hermes/skills/deep-tech-doc/md2html.py": "0ce66ae27b015f2d6153dacc3174c9db7a1a9a10944518914021a617f83af6b7",
    "/local_data/huguangyao/workspace/report-automation/render_catalog.py": "ea5212af3c295195b0077468eb5d14ea933a865756aabb94c011f4b16db3320e",
    "/local_data/huguangyao/workspace/news-briefings/_automation/render_news.py": "062f557781c5b602b962c995f73eb40d2f77c4905877a84114dbfd471aad5692",
    "/local_data/huguangyao/workspace/news-briefings/_automation/assets/md2html.py": "fe31a5af03437042981095ce99c637edeea589b1abeb216e7dc5b28789a20719",
    str(Path(__file__).with_name('memory_audit_artifacts.py')): 'b1df549c692a920a76320bc51c10463f5e900dc053ee35957c123b42ae14594e',
    **PUBLISHER_RESOURCE_SHA256,
}

_FROZEN_DEPENDENCY_IMPORT = '''\
import importlib.abc, importlib.util, os, pathlib, io, hashlib
_original_spec_from_file_location = importlib.util.spec_from_file_location
_original_path_open = pathlib.Path.open
_original_path_is_file = pathlib.Path.is_file
_original_path_resolve = pathlib.Path.resolve
_resource_roots = {str(pathlib.Path(path).parent) for path in _dependency_bytes}
_resource_outputs = set()
def _logical(path):
    return os.path.abspath(os.fspath(path))
def _allow_outputs(*paths):
    _resource_outputs.update(_logical(path) for path in paths)
def _protected_resource(path):
    return (path not in _resource_outputs and pathlib.Path(path).suffix in
            {'.py', '.css', '.js', '.html', '.txt', '.json', '.woff2'} and
            any(path == root or path.startswith(root + os.sep) for root in _resource_roots))
def _resource_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
    key = _logical(path)
    if key in _dependency_bytes:
        if mode not in ('r', 'rb', 'rt'):
            raise OSError('memory_audit_resource_read_only:' + key)
        data = _dependency_bytes[key]
        return io.BytesIO(data) if 'b' in mode else io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors=errors, newline=newline)
    if 'r' in mode and _protected_resource(key):
        raise OSError('memory_audit_unpinned_resource:' + key)
    return _original_path_open(path, mode, buffering, encoding, errors, newline)
def _resource_is_file(path, *args, **kwargs):
    key = _logical(path)
    if key in _dependency_bytes:
        return True
    return False if _protected_resource(key) else _original_path_is_file(path, *args, **kwargs)
def _resource_resolve(path, *args, **kwargs):
    key = _logical(path)
    return pathlib.Path(key) if key in _dependency_bytes else _original_path_resolve(path, *args, **kwargs)
pathlib.Path.open = _resource_open
pathlib.Path.is_file = _resource_is_file
pathlib.Path.resolve = _resource_resolve
def _offline_fonts(module):
    original = module.prepare_fonts
    def prepare(document, *args, **kwargs):
        selected, _ = module._font_subsets(document)
        pins = {}
        for relative, _ in selected:
            key = _logical(module.ASSETS / relative)
            if key not in _dependency_bytes:
                raise ValueError('memory_audit_unpinned_font:' + key)
            pins[relative] = hashlib.sha256(_dependency_bytes[key]).hexdigest()
        return original(document, allow_download=False, allow_fetch=False, pins=pins)
    module.prepare_fonts = prepare
class _SnapshotLoader(importlib.abc.Loader):
    def __init__(self, path, data):
        self.path, self.data = path, data
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        exec(compile(self.data, self.path, 'exec'), module.__dict__)
        if callable(getattr(module, 'prepare_fonts', None)) and callable(getattr(module, '_font_subsets', None)):
            _offline_fonts(module)
def _snapshot_spec(name, location=None, *, loader=None, submodule_search_locations=None):
    path = os.path.abspath(os.fspath(location)) if location is not None else None
    if path not in _dependency_bytes:
        raise ImportError('memory_audit_unpinned_dependency:' + str(path))
    loader = _SnapshotLoader(path, _dependency_bytes[path])
    return _original_spec_from_file_location(name, location, loader=loader,
                                             submodule_search_locations=submodule_search_locations)
importlib.util.spec_from_file_location = _snapshot_spec
'''

_VERIFIED_CATALOG_BOOTSTRAP = '''\
import base64, json, sys
catalog_path = sys.argv[1]
payload = json.loads(sys.stdin.buffer.read())
_dependency_bytes = {path: base64.b64decode(data, validate=True)
                     for path, data in payload['dependencies'].items()}
catalog_bytes = base64.b64decode(payload['catalog'], validate=True)
''' + _FROZEN_DEPENDENCY_IMPORT + '''\
helper_path = payload['artifact_helper']
spec = importlib.util.spec_from_file_location('memory_audit_artifacts', helper_path)
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)
result = artifacts.run_catalog(catalog_path, payload, _dependency_bytes, _allow_outputs)
print(json.dumps(result))
'''

_VERIFIED_PUBLISHER_BOOTSTRAP = '''\
import base64, json, sys
script, _site_packages, interpreter_fd = sys.argv[1:4]
code_length = int.from_bytes(sys.stdin.buffer.read(8), 'big')
code = sys.stdin.buffer.read(code_length)
payload = json.loads(sys.stdin.buffer.read())
source_bytes = base64.b64decode(payload['source'], validate=True)
_dependency_bytes = {path: base64.b64decode(data, validate=True)
                     for path, data in payload['dependencies'].items()}
''' + _FROZEN_DEPENDENCY_IMPORT + f'''\
_catalog_bootstrap = {_VERIFIED_CATALOG_BOOTSTRAP!r}
''' + '''\
helper_path = payload.get('artifact_helper')
artifacts = None
if helper_path is not None:
    spec = importlib.util.spec_from_file_location('memory_audit_artifacts', helper_path)
    artifacts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifacts)
sys.argv = [script] + sys.argv[4:]
exec(compile(code, script, 'exec'), {'__name__': '__main__', '__file__': script,
                                   '__memory_audit_source_bytes__': source_bytes,
                                   '__memory_audit_interpreter_fd__': int(interpreter_fd),
                                   '__memory_audit_dependencies__': _dependency_bytes,
                                   '__memory_audit_allow_outputs__': _allow_outputs,
                                   '__memory_audit_artifacts__': artifacts,
                                   '__memory_audit_artifact_path__': helper_path,
                                   '__memory_audit_creation_baseline__': payload.get('creation_baseline'),
                                   '__memory_audit_catalog_bootstrap__': _catalog_bootstrap})
'''

_SUPERVISED_PUBLISHER_BOOTSTRAP = '''\
import ctypes, os, signal, socket, struct, sys, threading
PR_SET_PDEATHSIG = 1
if (not sys.platform.startswith('linux') or os.getpid() != 1
        or ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0):
    raise RuntimeError('memory_audit_publisher_supervisor_unavailable')
control = socket.socket(fileno=int(sys.argv.pop(1)))
lock_count = int(sys.argv.pop(1))
lock_fds = [int(sys.argv.pop(1)) for _ in range(lock_count)]
script = sys.argv.pop(1)
site_packages = sys.argv.pop(1)
interpreter_fd = sys.argv.pop(1)
sys.argv = ['-c', script, site_packages, interpreter_fd] + sys.argv[1:]
pidfd = os.pidfd_open(1)
control.sendmsg([b'READY'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('i', pidfd))])
os.close(pidfd)
if control.recv(8) != b'GO':
    raise RuntimeError('memory_audit_publisher_supervisor_unconfirmed')
publisher_pid = os.fork()
if publisher_pid:
    def watch_owner():
        while control.recv(1):
            pass
        os._exit(125)
    threading.Thread(target=watch_owner, daemon=True).start()
    publisher_status = None
    while True:
        try:
            child_pid, status = os.wait()
        except ChildProcessError:
            break
        if child_pid == publisher_pid:
            publisher_status = os.waitstatus_to_exitcode(status)
    if publisher_status is None:
        raise RuntimeError('memory_audit_publisher_status_unavailable')
    sys.exit(publisher_status if publisher_status >= 0 else 128 - publisher_status)
control.close()
for fd in lock_fds:
    try:
        os.close(fd)
    except OSError:
        pass
''' + _VERIFIED_PUBLISHER_BOOTSTRAP


_LOCK_GUARDIAN_BOOTSTRAP = '''\
import os, select, socket, sys
control = socket.socket(fileno=int(sys.argv[1]))
pidfd = int(sys.argv[2])
lock_fds = [int(value) for value in sys.argv[3:]]
for fd in lock_fds:
    os.fstat(fd)
poller = select.poll()
poller.register(pidfd, select.POLLIN)
control.send(b'READY')
control.close()
while True:
    try:
        events = poller.poll(1000)
        if any(fd == pidfd and mask & select.POLLIN for fd, mask in events):
            break
    except InterruptedError:
        continue
'''


def _publisher_environment():
    env = {key: os.environ[key] for key in ('HOME', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'TZ')
           if key in os.environ}
    env.update({'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})
    return env


def _receive_supervisor(parent, proc, pidfd):
    received_pidfds = []
    supervisor_fd = None
    try:
        data, ancillary, flags, _ = parent.recvmsg(
            16, socket.CMSG_SPACE(struct.calcsize('3i')) + socket.CMSG_SPACE(struct.calcsize('i')))
        credentials = []
        for level, kind, value in ancillary:
            if level != socket.SOL_SOCKET:
                continue
            if kind == socket.SCM_CREDENTIALS:
                credentials.append(struct.unpack('3i', value[:struct.calcsize('3i')]))
            elif kind == socket.SCM_RIGHTS:
                width = struct.calcsize('i')
                received_pidfds.extend(struct.unpack(f'{len(value) // width}i', value))
        if data != b'READY' or flags or len(credentials) != 1 or len(received_pidfds) != 1:
            raise OSError('memory_audit_publisher_supervisor_unconfirmed')
        pid, uid, gid = credentials[0]
        supervisor_fd = received_pidfds.pop()
        fdinfo = Path(f'/proc/self/fdinfo/{supervisor_fd}').read_text(encoding='utf-8')
        pidfd_pid = next(int(line.split(':', 1)[1]) for line in fdinfo.splitlines() if line.startswith('Pid:'))
        status = Path(f'/proc/{pid}/status').read_text(encoding='utf-8')
        fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
        if (pidfd_pid != pid or uid != os.geteuid() or gid != os.getegid() or int(fields['PPid']) != proc.pid  # windows-footgun: ok -- Linux-only helper
                or fields['NSpid'].split()[-1] != '1' or proc.poll() is not None):
            raise OSError('memory_audit_publisher_supervisor_unconfirmed')
        return supervisor_fd
    except Exception as exc:
        if supervisor_fd is not None:
            received_pidfds.append(supervisor_fd)
        for received_fd in received_pidfds:
            try:
                os.close(received_fd)
            except OSError:
                pass
        raise OSError('memory_audit_publisher_supervisor_unconfirmed') from exc


def _wait_supervisor(pidfd, proc, timeout):
    poller = select.poll()
    try:
        poller.register(pidfd, select.POLLIN)
        for grace in (min(5, timeout), 5):
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                try:
                    if any(fd == pidfd and events & select.POLLIN
                           for fd, events in poller.poll(max(1, int((deadline - time.monotonic()) * 1000)))):
                        return True
                except InterruptedError:
                    continue
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # windows-footgun: ok -- Linux-only pidfd supervisor
            except ProcessLookupError:
                pass
            except OSError:
                return False
        return False
    except OSError:
        return False


def _start_lock_guardian(pidfd, lock_fds, interpreter_fd):
    from cron.scheduler_script import _fd_path

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    guardian = None
    try:
        parent.settimeout(5)
        argv = [_fd_path(interpreter_fd), '-I', '-S', '-B', '-c', _LOCK_GUARDIAN_BOOTSTRAP,
                str(child.fileno()), str(pidfd), *[str(fd) for fd in lock_fds]]
        guardian = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(interpreter_fd, child.fileno(), pidfd, *lock_fds),
            start_new_session=True, env=_publisher_environment())  # detached-spawn: ok -- retains locks until pidfd exit
        child.close()
        if parent.recv(16) != b'READY':
            raise OSError('memory_audit_publisher_lock_guardian_unavailable')
        return guardian
    except (OSError, subprocess.SubprocessError) as exc:
        if guardian is not None:
            guardian.kill()
            guardian.wait(timeout=5)
        raise OSError('memory_audit_publisher_lock_guardian_unavailable') from exc
    finally:
        parent.close()
        child.close()


def _reap_publisher_process(proc):
    from agent.memory_provider import spawn_context_thread

    spawn_context_thread(proc.wait, name='memory-audit-publisher-reaper').start()


def _run_publisher_process(argv, input_data, timeout, interpreter_fd, lock_fds=()):
    from cron.scheduler_script import _fd_path, _open_verified_fd

    if not sys.platform.startswith('linux'):
        raise OSError('memory_audit_publisher_supervisor_unavailable')
    unshare_fd = None
    parent = child = None
    proc = None
    guardian = None
    pidfd = None
    communicated = False
    teardown_error = None
    try:
        unshare_fd = _open_verified_fd(Path('/usr/bin/unshare'), UNSHARE_SHA256, direct=True)
        if unshare_fd is None:
            raise OSError('memory_audit_publisher_supervisor_unavailable')
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        parent.settimeout(min(timeout, 10))
        namespace_argv = [_fd_path(unshare_fd), '--user', '--map-current-user', '--pid', '--fork', '--kill-child',
                          argv[0], *argv[1:6], str(child.fileno()), str(len(lock_fds)),
                          *[str(fd) for fd in lock_fds], *argv[6:]]
        env = _publisher_environment()
        inherited_fds = tuple(fd for fd in (interpreter_fd, unshare_fd, child.fileno(), *lock_fds)
                              if type(fd) is int and fd >= 0)
        proc = subprocess.Popen(namespace_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, pass_fds=inherited_fds,
                                start_new_session=True, env=env)  # detached-spawn: ok -- pidfd waits for namespace exit
        child.close()
        child = None
        pidfd = _receive_supervisor(parent, proc, pidfd)
        guardian = _start_lock_guardian(pidfd, lock_fds, interpreter_fd)
        parent.send(b'GO')
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        communicated = True
        return proc.returncode, stdout, stderr
    finally:
        if child is not None:
            child.close()
        if parent is not None:
            parent.close()
        if pidfd is not None:
            if not _wait_supervisor(pidfd, proc, timeout):
                teardown_error = OSError('memory_audit_publisher_teardown_unconfirmed')
            os.close(pidfd)
            pidfd = None
        if proc is not None:
            try:
                if teardown_error is not None and proc.poll() is None:
                    proc.kill()
                if not communicated:
                    proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                teardown_error = OSError('memory_audit_publisher_teardown_unconfirmed')
                _reap_publisher_process(proc)
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        if unshare_fd is not None:
            os.close(unshare_fd)
        if guardian is not None:
            if teardown_error is not None:
                _reap_publisher_process(guardian)
            else:
                try:
                    guardian.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    guardian.kill()
                    guardian.wait(timeout=5)
        if teardown_error is not None:
            raise teardown_error





def matches(job_id, home):
    expected_home = PROFILE_HOME or get_process_hermes_home()
    return job_id == JOB_ID and Path(home).resolve() == Path(expected_home).resolve()


def regular_path_under(path: Path, root: Path) -> bool:
    try:
        path = Path(os.path.abspath(path))
        root = Path(os.path.abspath(root))
        path.relative_to(root)
        if root.is_symlink():
            return False
        current = root
        for component in path.relative_to(root).parts:
            current = current / component
            if current.is_symlink():
                return False
        if not current.exists():
            return False
        return True
    except (OSError, ValueError):
        return False


def approved_interpreter_identity():
    try:
        from pm.environments import project_python
        path = project_python(Path(project_root_str())).resolve()
        if not path.is_file() or path.is_symlink():
            return None
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def read_creation_baseline(home, expected_sha256=None):
    home = Path(home).resolve()
    parent = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in Path(BASELINE_RELATIVE).parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open('baseline.json', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError('memory_audit_baseline_changed')
            data = stream.read(4_000_001)
        if (len(data) > 4_000_000
                or hashlib.sha256(data).hexdigest() != (BASELINE_SHA256 if expected_sha256 is None else expected_sha256)):
            raise OSError('memory_audit_baseline_changed')
        return data
    finally:
        os.close(parent)


def execution_binding(job, home, interpreter_sha256=None, interpreter_path=None):
    """Return the fixed audit job and script identity, or ``None``."""
    try:
        home = Path(home).resolve()
        if not matches(job.get('id'), home):
            return None
        if (job.get('name') != 'builtin-memory-daily-maintenance-trial'
                or hashlib.sha256(job['prompt'].encode()).hexdigest() != PROMPT_SHA256
                or job.get('script') != SCRIPT_NAME
                or job.get('interpreter') not in (None, '')
                or job.get('no_agent') is not False
                or job.get('enabled_toolsets') != ['memory', 'file', 'terminal', 'no_mcp']
                or job.get('context_from') != ['self']
                or job.get('deliver') != 'local'
                or job.get('timezone') != 'Asia/Shanghai'
                or job.get('workdir') != str(home / 'reports/builtin-memory-audit-trial-20261110')
                or job.get('schedule') != {'kind': 'cron', 'expr': '15 3 * * *', 'display': '15 3 * * *'}
                or interpreter_path is None or interpreter_sha256 is None
                or len(interpreter_sha256) != 64):
            return None
        approved = approved_interpreter_identity()
        if approved is None or (Path(interpreter_path).resolve(), interpreter_sha256) != approved:
            return None
        scripts_root = home / 'scripts'
        memories_root = home / 'memories'
        if (scripts_root.is_symlink() or memories_root.is_symlink()
                or not scripts_root.is_dir() or not memories_root.is_dir()):
            return None
        collector = scripts_root / SCRIPT_NAME
        publisher = home / 'scripts' / 'publish-builtin-memory-report.py'
        if (not regular_path_under(collector, scripts_root)
                or not regular_path_under(publisher, scripts_root)
                or not collector.is_file() or collector.is_symlink()
                or not publisher.is_file() or publisher.is_symlink()):
            return None
        collector_sha256 = hashlib.sha256(collector.read_bytes()).hexdigest()
        publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
        if collector_sha256 != COLLECTOR_SHA256 or publisher_sha256 != PUBLISHER_SHA256:
            return None
        read_creation_baseline(home)
        return {'collector': collector, 'publisher': publisher,
                'collector_sha256': collector_sha256, 'publisher_sha256': publisher_sha256,
                'interpreter_path': str(interpreter_path), 'interpreter_sha256': interpreter_sha256,
                'publisher_dependencies': dict(PUBLISHER_DEPENDENCY_SHA256),
                'baseline_sha256': BASELINE_SHA256}
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None


class RunPolicy:
    def __init__(self, status, snapshot_hashes, cancel_event):
        self.status = status
        self.snapshot_hashes = snapshot_hashes
        self.cancel_event = cancel_event
        self.lock = threading.RLock()
        self.active = True
        self.report_source_sha256: str | None = None
        self.publication_receipt: dict | None = None
        self.published_snapshot_hashes: dict[str, str] | None = None
        self.report_source: Path | None = None
        self.publisher: Path | None = None
        self.collector: Path | None = None
        self.run_time: str | None = None
        self.read_paths: set[Path] = set()
        self.collector_sha256: str | None = None
        self.publisher_sha256: str | None = None
        self.interpreter_path: Path | None = None
        self.interpreter_sha256: str | None = None
        self.publisher_dependencies: dict[str, str] = {}
        self.creation_baseline: dict[str, str] | None = None
        self.audit_lock_path: Path | None = None
        self.store = None
        self.run_lock_held = False
        self.successful_writes: dict[str, int] = {'memory': 0, 'user': 0}
        self.memory_dir_identity: tuple[int, int] | None = None

    def live(self):
        return self.active and (self.cancel_event is None or not self.cancel_event.is_set())

    def revoke(self):
        with self.lock:
            self.active = False

    def request_refusal(self):
        if not self.live():
            return 'memory_audit_run_ended'
        if self.status == 'complete':
            return 'memory_audit_invalid_or_unpinned_request'
        return 'memory_audit_collection_incomplete'


    def admits_request(self, args):
        if not self.live() or self.status == 'error':
            return False
        ops = args.get('operations') if isinstance(args, dict) else None
        if (not isinstance(ops, list) or not ops or any(not isinstance(op, dict) for op in ops)
                or args.get('target') not in ('memory', 'user')
                or args.get('action') or args.get('content') or args.get('old_text') or args.get('new_text')):
            return False
        if self.status == 'complete':
            return all(op.get('action') == 'add' or (op.get('action') in ('patch', 'remove')
                       and isinstance(op.get('matched_entry'), str) and bool(op['matched_entry'])) for op in ops)
        if self.status != 'partial' or len(ops) != 1:
            return False
        op = ops[0]
        content = op.get('content')
        return (set(op) == {'action', 'content'} and op['action'] == 'add'
                and isinstance(content, str) and bool(content) and content == content.strip())

    def admits_direct_mutation(self, target, action, matched_entry=None, operations=None):
        """Authorize store calls that bypass the public memory-tool dispatcher."""
        if not self.live() or self.status == 'error' or target not in ('memory', 'user'):
            return False
        if self.successful_writes.get(target, 0) >= 1:
            return False
        if action == 'batch':
            if not isinstance(operations, list) or not operations:
                return False
            if self.status == 'partial':
                return len(operations) == 1 and operations[0].get('action') == 'add'
            return all(op.get('action') == 'add' or (
                op.get('action') in ('patch', 'remove')
                and isinstance(op.get('matched_entry'), str) and bool(op['matched_entry']))
                for op in operations)
        if self.status == 'partial':
            return action == 'add'
        if action in ('patch', 'remove', 'replace'):
            return isinstance(matched_entry, str) and bool(matched_entry)
        return action == 'add'

    def report_action(self, name, args):
        if not self.live() or not isinstance(args, dict):
            return None
        if name == 'read_file':
            path = args.get('path')
            if (isinstance(path, str) and Path(path).is_absolute()
                    and path in {str(p) for p in getattr(self, 'read_paths', set())}
                    and set(args) <= {'path', 'offset', 'limit'}):
                return 'read_bound'
            return None
        source = self.report_source
        publisher = self.publisher
        run_time = self.run_time
        if source is None:
            return None
        if name == 'write_file':
            if (set(args) != {'path', 'content'} or args['path'] != str(source)
                    or not isinstance(args['content'], str)):
                return None
            return 'write_report'
        if name == 'terminal':
            if publisher is None or run_time is None:
                return None
            if (self.publisher_sha256 is None or publisher.is_symlink() or not publisher.is_file()
                    or hashlib.sha256(publisher.read_bytes()).hexdigest() != self.publisher_sha256):
                return None
            if (set(args) not in ({'command'}, {'command', 'timeout'})
                    or not isinstance(args['command'], str)):
                return None
            timeout = args.get('timeout', 180)
            if type(timeout) is not int or not 1 <= timeout <= 600:
                return None
            try:
                argv = shlex.split(args['command'])
            except ValueError:
                return None
            expected = [str(publisher), '--source', str(source), '--run-time', run_time]
            if len(argv) != 6 or argv[0] not in ('python', 'python3', sys.executable) or argv[1:] != expected:
                return None
            return 'publish_report'
        return None

    def run_report_action(self, action, args):
        if self.status == 'error':
            if self.audit_lock_path is None:
                return self._run_report_action(action, args)
            from tools.memory_tool_store import MemoryStore
            try:
                with MemoryStore._file_lock(self.audit_lock_path) as lock_fd:
                    lock_fds = [lock_fd] if isinstance(lock_fd, int) else []
                    return self._run_report_action(action, args, lock_fds=lock_fds)
            except (OSError, ValueError):
                return json.dumps({'error': 'memory_audit_snapshot_or_run_changed'})
        if action != 'publish_report':
            return self._run_report_action(action, args)
        if self.store is None or self.audit_lock_path is None:
            return json.dumps({'error': 'memory_audit_invalid_store_binding'})
        from tools.memory_tool_store import MemoryStore
        try:
            with ExitStack() as locks:
                lock_fds = [locks.enter_context(MemoryStore._file_lock(self.audit_lock_path))]
                locks.enter_context(self.lock)
                for target in ('memory', 'user'):
                    lock_fds.append(locks.enter_context(MemoryStore._file_lock(self.store._path_for(target))))
                if not self.live() or not self.matches_store(self.store):
                    return json.dumps({'error': 'memory_audit_snapshot_or_run_changed'})
                result = self._run_report_action(action, args, lock_fds=lock_fds)
                if not self.matches_store(self.store):
                    self.publication_receipt = None
                    self.published_snapshot_hashes = None
                    return json.dumps({'error': 'memory_audit_snapshot_or_run_changed'})
                return result
        except (OSError, ValueError):
            return json.dumps({'error': 'memory_audit_snapshot_or_run_changed'})

    def _publisher_dependency_bytes(self):
        if (self.publisher_sha256 == PUBLISHER_SHA256
                and self.publisher_dependencies != PUBLISHER_DEPENDENCY_SHA256):
            raise OSError('memory_audit_publisher_dependency_changed')
        snapshots = {}
        for dependency, digest in self.publisher_dependencies.items():
            data = self.read_verified_bytes(Path(dependency), digest)
            if len(digest) != 64 or data is None:
                raise OSError('memory_audit_publisher_dependency_changed')
            snapshots[dependency] = data
        return snapshots

    def _verify_creation_baseline(self):
        if self.creation_baseline is None:
            return
        collector = self.collector
        if collector is None:
            raise OSError('memory_audit_baseline_changed')
        baseline = read_creation_baseline(collector.parent.parent)
        expected = {'path': str(collector.parent.parent / BASELINE_RELATIVE),
                    'sha256': hashlib.sha256(baseline).hexdigest()}
        if self.creation_baseline != expected:
            raise OSError('memory_audit_baseline_changed')

    def _run_report_action(self, action, args, lock_fds=()):
        with self.lock:
            if not self.live():
                return json.dumps({'error': 'memory_audit_run_ended'})
            if action == 'read_bound':
                return self.read_bound(args)
            if action == 'write_report':
                source = self.report_source
                if source is None:
                    return json.dumps({'error': 'memory_audit_report_unbound'})
                parent_fd = None
                try:
                    if source.parent.is_symlink() or any(part.is_symlink() for part in source.parent.parents if part.exists()):
                        return json.dumps({'error': 'memory_audit_tool_scope_blocked', 'success': False})
                    parent_fd = self.open_directory(source.parent)
                    fd = os.open(source.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent_fd)
                    with os.fdopen(fd, 'w', encoding='utf-8') as out:
                        out.write(args['content'])
                        out.flush()
                        os.fsync(out.fileno())
                    self.report_source_sha256 = hashlib.sha256(args['content'].encode('utf-8')).hexdigest()
                    self.publication_receipt = None
                    self.published_snapshot_hashes = None
                    return json.dumps({'success': True, 'resolved_path': str(source)})
                except OSError as exc:
                    retry = self._idempotent_report_retry(source, parent_fd, args['content'], exc)
                    if retry is not None:
                        return retry
                    return json.dumps({'error': str(exc)})
                finally:
                    if parent_fd is not None:
                        os.close(parent_fd)
            self.publication_receipt = None
            self.published_snapshot_hashes = None
            source = self.report_source
            publisher = self.publisher
            run_time = self.run_time
            if source is None or publisher is None or run_time is None or not self.report_source_sha256:
                return json.dumps({'error': 'memory_audit_publication_unbound'})
            try:
                source_bytes = self.read_verified_bytes(source, self.report_source_sha256)
                if source_bytes is None:
                    return json.dumps({'error': 'memory_audit_report_source_changed'})
                publisher_bytes = self.read_verified_bytes(publisher, self.publisher_sha256)
                if publisher_bytes is None:
                    return json.dumps({'error': 'memory_audit_publisher_changed'})
                dependency_bytes = self._publisher_dependency_bytes()
                self._verify_creation_baseline()
                if (self.collector is not None
                        and (self.collector.is_symlink() or not self.collector.is_file()
                             or hashlib.sha256(self.collector.read_bytes()).hexdigest()
                             != self.collector_sha256)):
                    return json.dumps({'error': 'memory_audit_collector_changed'})
                from cron.scheduler_script import _fd_path, _open_verified_fd
                identity = approved_interpreter_identity()
                if (identity is None or self.interpreter_path is None or self.interpreter_sha256 is None
                        or identity != (self.interpreter_path, self.interpreter_sha256)):
                    return json.dumps({'error': 'memory_audit_interpreter_changed'})
                interpreter_fd = _open_verified_fd(self.interpreter_path, self.interpreter_sha256, direct=True)
                if interpreter_fd is None:
                    return json.dumps({'error': 'memory_audit_interpreter_changed'})
                try:
                    payload = json.dumps({
                        'source': base64.b64encode(source_bytes).decode('ascii'),
                        'creation_baseline': self.creation_baseline,
                        'artifact_helper': str(Path(__file__).with_name('memory_audit_artifacts.py'))
                            if str(Path(__file__).with_name('memory_audit_artifacts.py')) in dependency_bytes else None,
                        'dependencies': {path: base64.b64encode(data).decode('ascii')
                                         for path, data in dependency_bytes.items()},
                    }).encode('utf-8')
                    argv = [_fd_path(interpreter_fd), '-I', '-S', '-B', '-c', _SUPERVISED_PUBLISHER_BOOTSTRAP,
                        str(publisher), '', str(interpreter_fd),
                        '--source', str(source), '--run-time', run_time]
                    timeout = min(args.get('timeout', 180), 180)
                    returncode, stdout_raw, stderr_raw = _run_publisher_process(
                        argv, len(publisher_bytes).to_bytes(8, 'big') + publisher_bytes + payload,
                        timeout, interpreter_fd, lock_fds)
                finally:
                    os.close(interpreter_fd)
                stdout = stdout_raw.decode('utf-8', errors='replace')
                stderr = stderr_raw.decode('utf-8', errors='replace')
                if returncode == 0:
                    try:
                        self.publication_receipt = json.loads(stdout, object_pairs_hook=_strict_object)
                        self.published_snapshot_hashes = dict(self.snapshot_hashes)
                    except (ValueError, TypeError):
                        pass
                return json.dumps({'exit_code': returncode, 'output': stdout,
                                   'stderr': stderr})
            except (OSError, subprocess.SubprocessError) as exc:
                return json.dumps({'error': str(exc)})

    def report_verified(self, store=None):
        from tools.memory_tool_store import MemoryStore
        try:
            with ExitStack() as locks:
                if store is not None:
                    if self.audit_lock_path is None:
                        return False
                    locks.enter_context(MemoryStore._file_lock(self.audit_lock_path))
                locks.enter_context(self.lock)
                if store is not None:
                    for target in ('memory', 'user'):
                        locks.enter_context(MemoryStore._file_lock(store._path_for(target)))
                    if not self.matches_store(store):
                        return False
                    if not self._report_verified_unlocked():
                        return False
                    return self.matches_store(store)
                return self._report_verified_unlocked()
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return False

    def _report_verified_unlocked(self):
        try:
            source = self.report_source
            run_time = self.run_time
            if (not self.live() or source is None or run_time is None
                    or not self.report_source_sha256 or not isinstance(self.publication_receipt, dict)
                    or self.published_snapshot_hashes != self.snapshot_hashes):
                return False
            publisher = self.publisher
            if (publisher is None or publisher.is_symlink() or not publisher.is_file()
                    or self.publisher_sha256 is None
                    or hashlib.sha256(publisher.read_bytes()).hexdigest() != self.publisher_sha256):
                return False
            collector = self.collector
            if (collector is None or collector.is_symlink() or not collector.is_file()
                    or self.collector_sha256 is None
                    or hashlib.sha256(collector.read_bytes()).hexdigest() != self.collector_sha256):
                return False
            for dependency, digest in self.publisher_dependencies.items():
                path = Path(dependency)
                if (path.is_symlink() or not path.is_file()
                        or len(digest) != 64
                        or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                    return False
            receipt = self.publication_receipt
            if self.creation_baseline is not None:
                self._verify_creation_baseline()
                if receipt.get('creation_baseline') != self.creation_baseline:
                    return False
            stamp = datetime.fromisoformat(run_time).astimezone(ZoneInfo('Asia/Shanghai'))
            day = stamp.date()
            start = day - timedelta(days=(day.weekday() - 5) % 7)
            end = start + timedelta(days=6)
            target = REPORT_ROOT / f'{start}_{end}' / f'{day}' / f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'
            saved = source.parent / f'publication-{stamp:%Y%m%d-%H%M%S}.json'
            disk_receipt = json.loads(saved.read_text(encoding='utf-8-sig'), object_pairs_hook=_strict_object)
            if disk_receipt != receipt:
                return False
            if not self._catalog_verified(receipt, target):
                return False
            return (receipt['source'] == str(source.resolve())
                    and receipt['run_time'] == stamp.isoformat()
                    and receipt['source_sha256'] == self.report_source_sha256
                    and hashlib.sha256(source.read_bytes()).hexdigest() == self.report_source_sha256
                    and receipt['html'] == str(target)
                    and receipt['report_readback_verified'] is True
                    and type(receipt['catalog_exit_code']) is int and receipt['catalog_exit_code'] == 0
                    and receipt['library_indexed'] is True
                    and isinstance(receipt['library_document_id'], str) and bool(receipt['library_document_id'])
                    and receipt['html_sha256'] == receipt['library_preview_sha256']
                    and hashlib.sha256(target.read_bytes()).hexdigest() == receipt['html_sha256'])
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return False

    def _catalog_verified(self, receipt, target):
        from cron.memory_audit_artifacts import catalog_readback, raw_manifest, CATALOG_RECEIPT
        resources = raw_manifest(self._publisher_dependency_bytes())
        catalog = catalog_readback(REPORT_ROOT, CATALOG_RECEIPT, target, resources,
                                   receipt['catalog_inputs'])
        return all(receipt.get(key) == value for key, value in catalog.items())

    @staticmethod
    def open_directory(path):
        parts = Path(path).resolve().parts
        fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def read_verified_bytes(path, expected_sha256):
        if not expected_sha256:
            return None
        parent_fd = None
        file_fd = None
        try:
            resolved = Path(path).resolve()
            parent_fd = RunPolicy.open_directory(resolved.parent)
            file_fd = os.open(resolved.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                return None
            with os.fdopen(file_fd, 'rb') as handle:
                file_fd = None
                data = handle.read()
            return data if hashlib.sha256(data).hexdigest() == expected_sha256 else None
        except OSError:
            return None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if parent_fd is not None:
                os.close(parent_fd)

    def _idempotent_report_retry(self, source, parent_fd, content, error):
        if error.errno != errno.EEXIST or parent_fd is None:
            return None
        payload = content.encode('utf-8')
        try:
            existing_fd = os.open(source.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
            if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                os.close(existing_fd)
                return None
            with os.fdopen(existing_fd, 'rb') as existing:
                if existing.read(len(payload) + 1) != payload:
                    return None
            digest = hashlib.sha256(payload).hexdigest()
            self.report_source_sha256 = digest
            receipt_matches = (isinstance(self.publication_receipt, dict)
                               and self.publication_receipt.get('source_sha256') == digest
                               and self.published_snapshot_hashes == self.snapshot_hashes)
            if not receipt_matches:
                self.publication_receipt = None
                self.published_snapshot_hashes = None
            return json.dumps({'success': True, 'resolved_path': str(source), 'idempotent': True})
        except OSError:
            return None

    def read_bound(self, args):
        offset, limit = args.get('offset', 1), args.get('limit', 2000)
        if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 2000:
            return json.dumps({'error': 'memory_audit_invalid_read_range'})
        fd = None
        try:
            path = Path(args['path'])
            if (path.parent.is_symlink()
                    or any(part.is_symlink() for part in path.parent.parents if part.exists())):
                raise OSError('symlinked bound path')
            fd = self.open_directory(path.parent)
            child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4_000_000:
                raise OSError('not a bounded regular file')
            with os.fdopen(fd, 'r', encoding='utf-8-sig') as handle:
                fd = None
                content = handle.read(4_000_001)
            if len(content) > 4_000_000:
                raise OSError('file grew beyond the bound')
            lines = content.splitlines()
            selected = lines[offset - 1:offset - 1 + limit]
            output = []
            length = 0
            for number, line in enumerate(selected, offset):
                entry = f'{number}|{line.rstrip(chr(10)).rstrip(chr(13))}'
                if length + len(entry) > 100_000:
                    if not output:
                        return json.dumps({'error': 'memory_audit_read_line_too_long', 'success': False})
                    break
                output.append(entry)
                length += len(entry)
            result = {'content': '\n'.join(output), 'total_lines': len(lines)}
            next_offset = offset + len(output)
            if next_offset <= len(lines):
                result['next_offset'] = next_offset
            return json.dumps(result, ensure_ascii=False)
        except (OSError, UnicodeError, ValueError):
            return json.dumps({'error': 'memory_audit_tool_scope_blocked', 'success': False})
        finally:
            if fd is not None:
                os.close(fd)

    def matches_store(self, store):
        try:
            for target in ('memory', 'user'):
                path = store._path_for(target)
                parent_info = os.lstat(path.parent)
                if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                        or path.is_symlink() or not path.is_file()):
                    return False
                identity = (parent_info.st_dev, parent_info.st_ino)
                if self.memory_dir_identity is None:
                    self.memory_dir_identity = identity
                if identity != self.memory_dir_identity:
                    return False
                if hashlib.sha256(path.read_bytes()).hexdigest() != self.snapshot_hashes.get(target):
                    return False
            return True
        except OSError:
            return False

    def matches_final_store(self, store):
        """Re-read both files under the policy lock for report closeout."""
        return self.matches_store(store)

    def final_store_verified(self, store):
        with self.lock:
            return self.live() and self.matches_final_store(store)

    def matches_store_after_write(self, store, target, content):
        try:
            for name in ('memory', 'user'):
                path = store._path_for(name)
                parent_info = os.lstat(path.parent)
                if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                        or path.is_symlink() or not path.is_file()):
                    return False
                if self.memory_dir_identity != (parent_info.st_dev, parent_info.st_ino):
                    return False
                expected = content if name == target else None
                expected_hash = (hashlib.sha256(expected).hexdigest() if expected is not None
                                 else self.snapshot_hashes.get(name))
                if expected_hash is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    return False
            return True
        except OSError:
            return False

    def note_write(self, target, content):
        if self.published_snapshot_hashes is not None:
            self.publication_receipt = None
            self.published_snapshot_hashes = None
        self.snapshot_hashes[target] = hashlib.sha256(content).hexdigest()
        self.successful_writes[target] = self.successful_writes.get(target, 0) + 1

    def written_bytes(self, target, path):
        expected = self.snapshot_hashes.get(target)
        data = path.read_bytes()
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('memory_audit_written_bytes_changed')
        return data

    def bind_store_identity(self, store):
        try:
            path = store._path_for('memory').parent
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return False
            self.memory_dir_identity = (info.st_dev, info.st_ino)
            self.store = store
            return True
        except OSError:
            return False

    def matches_source(self, target, source):
        return (self.live() and target in self.snapshot_hashes
                and hashlib.sha256(source).hexdigest() == self.snapshot_hashes[target])

    @staticmethod
    def has_nonidentical_duplicates(source):
        body = source.removeprefix(b'\xef\xbb\xbf')
        pieces = re.split(rb'(?:\r\n|\r|\n)\xc2\xa7(?:\r\n|\r|\n)', body)
        if pieces:
            while pieces[-1].endswith((b'\r', b'\n')):
                pieces[-1] = pieces[-1][:-1]
        seen = {}
        for piece in pieces:
            key = piece.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').strip()
            if key:
                if key in seen and seen[key] != piece:
                    return True
                seen[key] = piece
        return False

    def admits_result(self, target, raw, result):
        if not self.live() or target not in self.snapshot_hashes:
            return False
        return self.status == 'complete'

    def lossless_dedup(self, target, source, result):
        if (not self.live() or self.status != 'partial' or target not in self.snapshot_hashes
                or hashlib.sha256(source).hexdigest() != self.snapshot_hashes[target]):
            return None
        from tools.memory_tool_store import MemoryStore
        try:
            source.decode('utf-8-sig')
        except UnicodeDecodeError:
            return None
        bom = b'\xef\xbb\xbf' if source.startswith(b'\xef\xbb\xbf') else b''
        body = source[len(bom):]
        windows_delimiter, unix_delimiter = b'\r\n\xc2\xa7\r\n', b'\n\xc2\xa7\n'
        if windows_delimiter in body and unix_delimiter in body:
            return None
        delimiter = windows_delimiter if windows_delimiter in body else unix_delimiter
        pieces = body.split(delimiter)
        if len(pieces) < 2:
            return None
        suffix = b''
        while pieces[-1].endswith(b'\n'):
            newline = b'\r\n' if pieces[-1].endswith(b'\r\n') else b'\n'
            pieces[-1] = pieces[-1][:-len(newline)]
            suffix = newline + suffix
        unique = list(dict.fromkeys(pieces))
        if not all(pieces) or len(unique) == len(pieces):
            return None
        output = bom + delimiter.join(unique) + suffix
        normalized = output.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        return output if MemoryStore._parse_entries(normalized) == result else None


@contextmanager
def hold_audit_run_lock(home):
    from tools.memory_tool_store import MemoryStore
    path = Path(home) / 'cron' / '.memory-audit.lock'
    with MemoryStore._file_lock(path):
        yield


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def prompt_output(output):
    from tools.threat_patterns import INVISIBLE_CHARS
    return ''.join(f'\\u{ord(char):04x}' if char in INVISIBLE_CHARS else char for char in output)


def publication_binding(data, home):
    try:
        stamp = datetime.fromisoformat(data['observed_at'])
        if stamp.tzinfo is None:
            return None
        stamp = stamp.astimezone(ZoneInfo('Asia/Shanghai'))
        source = Path(home) / 'reports/builtin-memory-audit-trial-20261110' / f'maintenance-{stamp:%Y%m%d-%H%M%S}.md'
        publisher = Path(home) / 'scripts/publish-builtin-memory-report.py'
        publication = data['publication']
        collector = Path(home) / 'scripts' / SCRIPT_NAME
        if (publication['source_markdown'] != str(source) or publication['publisher'] != str(publisher)
                or publication['run_time'] != data['observed_at']
                or collector.is_symlink() or not collector.is_file()
                or hashlib.sha256(collector.read_bytes()).hexdigest() != COLLECTOR_SHA256
                or publisher.is_symlink() or not publisher.is_file()
                or hashlib.sha256(publisher.read_bytes()).hexdigest() != PUBLISHER_SHA256):
            return None
        return source, publisher, data['observed_at']
    except (KeyError, OSError, TypeError, ValueError):
        return None


def report_html_path(run_time):
    stamp = datetime.fromisoformat(run_time).astimezone(ZoneInfo('Asia/Shanghai'))
    day = stamp.date()
    start = day - timedelta(days=(day.weekday() - 5) % 7)
    end = start + timedelta(days=6)
    return REPORT_ROOT / f'{start}_{end}' / f'{day}' / f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'


def failure_report(home, reason):
    stamp = datetime.now(ZoneInfo('Asia/Shanghai'))
    home = Path(home)
    source = home / 'reports/builtin-memory-audit-trial-20261110' / f'maintenance-{stamp:%Y%m%d-%H%M%S}.md'
    policy = RunPolicy('error', {}, None)
    policy.audit_lock_path = home / 'cron' / '.memory-audit.lock'
    policy.report_source = source
    policy.publisher = home / 'scripts/publish-builtin-memory-report.py'
    policy.run_time = stamp.isoformat()
    if (policy.publisher.is_symlink() or not policy.publisher.is_file()
            or hashlib.sha256(policy.publisher.read_bytes()).hexdigest() != PUBLISHER_SHA256):
        return None, 'failure_report_publish_failed'
    policy.publisher_sha256 = PUBLISHER_SHA256
    policy.publisher_dependencies = dict(PUBLISHER_DEPENDENCY_SHA256)
    identity = approved_interpreter_identity()
    if identity is None:
        return None, 'failure_report_publish_failed'
    policy.interpreter_path, policy.interpreter_sha256 = identity
    body = ('---\ntitle: 记忆维护失败\n---\n\n# 记忆维护失败\n\n## 检查结果\n'
            f'本次维护未完成：{reason}。未修改 MEMORY.md 与 USER.md；外部并发变化未核实。\n'
            '来源覆盖和备份状态未能确认，不能依据本轮历史更新记忆。\n')
    written = json.loads(policy.run_report_action('write_report', {'content': body}))
    if not written.get('success'):
        return None, 'failure_report_source_unavailable'
    published = json.loads(policy.run_report_action('publish_report', {}))
    if published.get('exit_code') != 0:
        return None, 'failure_report_publish_failed'
    try:
        receipt = json.loads(published['output'], object_pairs_hook=_strict_object)
        day = stamp.date()
        start = day - timedelta(days=(day.weekday() - 5) % 7)
        end = start + timedelta(days=6)
        target = REPORT_ROOT / f'{start}_{end}' / f'{day}' / f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if (receipt['html'] != str(target) or receipt['report_readback_verified'] is not True
                or receipt['library_indexed'] is not True or not receipt['library_document_id']
                or receipt['html_sha256'] != digest or receipt['library_preview_sha256'] != digest
                or not policy._catalog_verified(receipt, target)):
            raise ValueError('report receipt mismatch')
        return target, None
    except (KeyError, OSError, TypeError, ValueError):
        return None, 'failure_report_readback_failed'


def collection_status(prerun, prompt, home):
    """Prototype: only accept a successful, intact script result and verified backup."""
    try:
        success, output = prerun
        if (success is not True or not isinstance(output, str) or not output
                or not isinstance(prompt, str) or prompt_output(output) not in prompt):
            return "error"
        data = json.loads(output, object_pairs_hook=_strict_object)
        history = data["history"]
        if (data["schema"] != "builtin-memory-audit-input.v1"
                or Path(data["profile_home"]).resolve() != Path(home).resolve()
                or data["collection_status"] not in ("complete", "partial")
                or type(history["complete_within_declared_scope"]) is not bool):
            return "error"
        status = data["collection_status"]
        if (status == "complete") != history["complete_within_declared_scope"]:
            return "error"
        baseline_bytes = read_creation_baseline(home)
        if data['creation_baseline'] != {'path': str(Path(home).resolve() / BASELINE_RELATIVE),
                                         'sha256': hashlib.sha256(baseline_bytes).hexdigest()}:
            return 'error'
        counters = ("eligible_messages", "included_messages", "omitted_messages", "conflict_count",
                    "uncertain_source_count", "omitted_conflicts", "omitted_uncertain_sources")
        if any(type(history[k]) is not int or history[k] < 0 for k in counters):
            return "error"
        if (history["included_messages"] + history["omitted_messages"] + history["uncertain_source_count"]
                != history["eligible_messages"]
                or len(history["messages"]) != history["included_messages"]):
            return "error"
        if status == "complete" and any(history[k] for k in counters[2:]):
            return "error"
        backup = Path(data["verified_backup"])
        backup_root = Path(home) / "reports/builtin-memory-audit-trial-20261110/backups"
        if backup.resolve().parent != backup_root.resolve():
            return "error"
        manifest = json.loads((backup / "manifest.json").read_text(encoding='utf-8-sig'), object_pairs_hook=_strict_object)
        if manifest["verified"] is not True:
            return "error"
        for name in ("MEMORY.md", "USER.md"):
            source = backup / name
            if source.is_symlink() or not source.is_file():
                return "error"
            value = hashlib.sha256(source.read_bytes()).hexdigest()
            if value != manifest["files"][name]["sha256"] or value != data["files"][name]["sha256"]:
                return "error"
        return status
    except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return "error"
