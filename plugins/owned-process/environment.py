"""Owned command transport: per-call bounded spools, no host execution fallback."""
import base64
from collections import deque
import json
import os
from pathlib import Path
import shlex
import subprocess
import threading
import uuid
import weakref

from tools.environments.base import BaseEnvironment

WORKER_ROOT = Path(__file__).resolve().parent


def canonical_directory(value, *, create=False):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError('owned paths must be absolute canonical directories without symlinks')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path == Path('/'):
        raise ValueError('owned path must be an explicit existing non-root directory')
    return path


def protect_runtime(*roots):
    """Reject writable aliases, including hardlinks and directory bind mounts.

    The host owner/configuration is trusted. Payloads cannot create links across
    the separate boundary mount or mount new aliases after capability removal.
    Do not follow host symlinks: they resolve in the payload's mount namespace.
    """
    def identity(path):
        info = path.stat()
        return info.st_dev, info.st_ino
    protected = {identity(WORKER_ROOT)}
    for path, directories, files in os.walk(WORKER_ROOT):
        protected.add(identity(Path(path)))
        protected.update(identity(Path(path) / name) for name in files)
    for root in roots:
        root = Path(root)
        if root == WORKER_ROOT or root in WORKER_ROOT.parents or WORKER_ROOT in root.parents:
            raise ValueError('writable root overlaps trusted runtime')
        pending, visited = [root], set()
        while pending:
            directory = pending.pop()
            key = identity(directory)
            if key in protected:
                raise ValueError('writable directory aliases trusted runtime')
            if key in visited:
                continue
            visited.add(key)
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if (info.st_dev, info.st_ino) in protected:
                        raise ValueError('writable path aliases trusted runtime')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))


def launch_argv(project, state, network):
    if network not in ('private', 'hostnet'):
        raise ValueError('network must be private or hostnet')
    project, state = canonical_directory(project), canonical_directory(state)
    protect_runtime(project, state)
    argv = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
    if network == 'hostnet':
        argv += ['--share-net']
    argv += ['--ro-bind', '/usr', '/usr']
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        path = Path('/') / name
        if path.is_symlink():
            argv += ['--symlink', os.readlink(path), '/' + name]
        elif path.exists():
            argv += ['--ro-bind', str(path), '/' + name]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',  # no-tmp: ok — sandbox-only tmpfs
             '--dir', '/run', '--dir', '/home/sandbox', '--dir', '/etc',
             '--bind', str(project), '/workspace', '--bind', str(state), '/state',
             '--ro-bind', str(WORKER_ROOT), '/boundary']
    if network == 'hostnet':
        for value in ('/etc/hosts', '/etc/resolv.conf', '/etc/nsswitch.conf', '/etc/ssl/certs'):
            if Path(value).exists():
                argv += ['--ro-bind', value, value]
    argv += ['--remount-ro', '/', '--chdir', '/workspace', '--clearenv',
             '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/home/sandbox',
             '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
             '--', '/usr/bin/python3', '-I', '-B', '/boundary/worker.py']
    return argv


class RemoteHandle:
    """A bounded per-call spool. Only its private pump waits for its consumer.

    The demultiplexer never writes to a consumer pipe. Terminal status belongs
    to the remote lifecycle and is independent of stdout consumption. The bound
    is unread material, not a cap on an actively drained full-fidelity stream.
    """
    SPOOL_LIMIT = 8 * 1024 * 1024

    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.returncode = None
        self.done = threading.Event()
        self._condition = threading.Condition()
        self._chunks = deque()
        self._queued = 0
        self._peak = 0
        self._diagnostic_chunk = None
        self._finished = False
        self._overflow = False
        self.diagnostic = None
        self.lifecycle = None
        read_fd, self.write_fd = os.pipe()
        self.stdout = None
        try:
            os.set_blocking(self.write_fd, False)
            self.stdout = os.fdopen(read_fd,'r',encoding='utf-8',errors='replace')  # windows-footgun: ok — binary command stream decoded incrementally, not a BOM-bearing file
            self._pump = threading.Thread(target=self._pump_output,daemon=True)
            self._pump.start()
        except BaseException as exc:
            # Construction owns both ends until the pump successfully starts.
            for close in (lambda: os.close(self.write_fd),
                          self.stdout.close if self.stdout is not None else lambda: os.close(read_fd)):
                try:
                    close()
                except OSError as cleanup_error:
                    exc.add_note('owned pipe rollback failed: ' + repr(cleanup_error))
            raise

    def output(self, raw):
        with self._condition:
            if self._overflow:
                return
            if self._queued + len(raw) > self.SPOOL_LIMIT:
                self._overflow = True
                self.diagnostic = 'output_backpressure_limit: unread spool exceeded 8388608 bytes; call cancelled'
                self._diagnostic_chunk = ('\n[' + self.diagnostic + ']\n').encode()
                # A slow control writer cannot park the sole event reader either.
                threading.Thread(target=self.kill,daemon=True).start()
            else:
                self._chunks.append(raw)
                self._queued += len(raw)
                self._peak = max(self._peak,self._queued)
            self._condition.notify()

    def _pump_output(self):
        import select
        waiter=select.poll()
        waiter.register(self.write_fd,select.POLLOUT)
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda:self._chunks or self._finished or self.owner.closed)
                    if not self._chunks and self._diagnostic_chunk is not None:
                        self._chunks.append(self._diagnostic_chunk)
                        self._queued += len(self._diagnostic_chunk)
                        self._diagnostic_chunk = None
                    if self.owner.closed or (not self._chunks and self._finished):
                        return
                    chunk = self._chunks[0]
                try:
                    count = os.write(self.write_fd,chunk)
                except BlockingIOError:
                    waiter.poll(50)
                    continue
                with self._condition:
                    self._queued -= count
                    if count == len(chunk):
                        self._chunks.popleft()
                    else:
                        self._chunks[0] = chunk[count:]
        except (BrokenPipeError,OSError):
            pass
        finally:
            with self._condition:
                self._chunks.clear()
                self._queued=0
                self._diagnostic_chunk=None
            os.close(self.write_fd)

    def finish(self, code, lifecycle=None):
        with self._condition:
            if self.done.is_set():
                return
            self.lifecycle = lifecycle
            self.returncode = 122 if self._overflow else code
            self._finished = True
            self.done.set()
            self._condition.notify_all()

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def kill(self):
        if not self.done.is_set():
            self.owner._cancel_handle(self)

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('namespace command',timeout)
        return self.returncode


class OwnedProjectEnvironment(BaseEnvironment):
    uses_host_filesystem = False
    is_local = False
    _passwordless_sudo_probe_supported = False

    def __init__(self, project, state, network='private', cwd='/workspace', timeout=20):
        self.closed = False
        self.project = canonical_directory(project)
        self.state = Path(state)
        self.state.mkdir(parents=False, exist_ok=False, mode=0o700)
        self._initialize_transport(lambda: launch_argv(self.project, self.state, network), cwd, timeout)

    def _initialize_transport(self, argv, cwd, timeout):
        """Start the existing owned wire over an explicitly prepared transport."""
        self.handles = {}
        self.all_handles = weakref.WeakSet()
        self.handles_lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.ready = threading.Event()
        self.receipt = None
        self.cleanup_receipts = []
        self.proc = None
        self.reader = None
        self.cleanup_complete = False
        self.cleanup_errors = []
        try:
            self.argv = argv()
            self.stderr_path = self.state / 'worker.stderr'
            self.stderr_file = self.stderr_path.open('w', encoding='utf-8')
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr_file, text=True, encoding='utf-8', errors='replace', close_fds=True,
                cwd=self.state, env={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1'},
                start_new_session=True)  # detached-spawn: ok — retained Popen, namespace teardown and cleanup
            self.reader = threading.Thread(target=self._read, name='owned-demultiplexer', daemon=True)
            self.reader.start()
            if not self.ready.wait(10) or self.receipt is None:
                raise RuntimeError('owned namespace worker failed: ' + self.stderr_path.read_text(encoding='utf-8-sig'))
            super().__init__(cwd=cwd, timeout=timeout, persist_env=True)
            self.init_session()
            if not self._snapshot_ready:
                raise RuntimeError('owned session bootstrap failed')
        except BaseException as exc:
            try:
                self.cleanup()
            except RuntimeError as cleanup_error:
                exc.add_note('owned constructor cleanup: ' + repr(cleanup_error))
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                event = json.loads(line)
                if event.get('event') == 'ready':
                    self.receipt = event
                    self.ready.set()
                    continue
                with self.handles_lock:
                    handle = self.handles.get(event.get('id'))
                if handle is None:
                    continue
                if event.get('event') == 'output':
                    handle.output(base64.b64decode(event['data'], validate=True))
                elif event.get('event') == 'started':
                    handle.scope = event['scope']
                elif event.get('event') == 'exit':
                    handle.finish(event['code'], event.get('lifecycle'))
                    with self.handles_lock:
                        self.handles.pop(event['id'], None)
        finally:
            self.ready.set()
            with self.handles_lock:
                for handle in list(self.handles.values()):
                    # A transport exit without a trusted reaper receipt stays unverified.
                    handle.finish(125)
                self.handles.clear()

    def _send(self, message):
        with self.send_lock:
            if self.closed or self.proc is None or self.proc.poll() is not None:
                raise RuntimeError('owned namespace closed; refusing host fallback')
            self.proc.stdin.write(json.dumps(message, separators=(',', ':')) + '\n')
            self.proc.stdin.flush()

    def _cancel_handle(self, handle):
        self._send({'op':'cancel', 'id':handle.identifier})

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None, _lifetime='command'):
        if _lifetime not in ('command', 'background'):
            raise ValueError('invalid owned process lifetime')
        identifier = uuid.uuid4().hex
        with self.handles_lock:
            if sum(not h._finished or h._pump.is_alive() for h in self.all_handles) >= 32:
                raise RuntimeError('owned process handle limit 32; drain or close previous handles')
            handle = RemoteHandle(self, identifier)
            self.all_handles.add(handle)
            self.handles[identifier] = handle
        try:
            self._send({'op':'run', 'id':identifier, 'command':cmd_string, 'stdin':stdin_data, 'lifetime':_lifetime})
        except BaseException:
            with self.handles_lock:
                self.handles.pop(identifier, None)
            handle.finish(125)
            handle.stdout.close()
            raise
        return handle

    def start_owned_process(self, command, *, cwd=None, lifetime='background'):
        if lifetime != 'background':
            raise ValueError('unsupported owned lifetime')
        prefix = (f'. {shlex.quote(self._snapshot_path)}\n' if self._snapshot_ready else '')
        script = prefix + f'cd {shlex.quote(cwd or self.cwd)} || exit 126\n' + command
        return self._run_bash(script, _lifetime='background')

    def _validate_handle(self, handle):
        if getattr(handle, 'owner', None) is not self or handle not in self.all_handles:
            raise ValueError('foreign or stale owned handle generation')

    def wait_owned(self, handle, timeout=None):
        self._validate_handle(handle)
        handle.wait(timeout)
        receipt = handle.lifecycle
        if (not receipt or receipt.get('owner_generation') != handle.identifier
                or receipt.get('root_exit') is None or receipt.get('descendant_empty') is not True
                or receipt.get('waitpid_echild') is not True or receipt.get('launcher_exit') != 0):
            raise RuntimeError('owned process terminal state unverified')
        return dict(receipt)

    def signal_owned(self, handle):
        self._validate_handle(handle)
        handle.kill()
        return self.wait_owned(handle, 5)

    def _kill_process(self, proc):
        self.signal_owned(proc)

    def execute(self, *args, **kwargs):
        result = super().execute(*args, **kwargs)
        if 'returncode' in result:
            lifecycle = result.get('owned_lifecycle') or {}
            if lifecycle.get('root_exit') is None or lifecycle.get('descendant_empty') is not True:
                raise RuntimeError('owned lifecycle unverified; no terminal completion may be published')
        return result

    def _wait_for_process(self, proc, *args, **kwargs):
        result = super()._wait_for_process(proc, *args, **kwargs)
        if proc.done.is_set():
            result['owned_lifecycle'] = self.wait_owned(proc, 0)
        return result

    def get_temp_dir(self):
        return '/state/tmp'

    def exec_prefix(self, *, user=None, interactive=True):
        return None

    def _stop_launcher(self):
        proc = getattr(self, 'proc', None)
        if proc is None or proc.poll() is not None:
            return
        try:
            self._send({'op':'shutdown'})
        except (OSError, RuntimeError, ValueError):
            # Keep waiting/reaping even if the control channel is already lost.
            proc.kill()
            proc.wait(5)
            raise
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired as exc:
            proc.kill()  # retained launcher only; PID namespace teardown, no host PID lookup
            proc.wait(5)
            raise RuntimeError('owned cleanup needed emergency namespace teardown') from exc

    @staticmethod
    def _join_started(thread):
        # ident remains set after termination; Thread creation alone owns no thread.
        if thread is not None and thread.ident is not None:
            thread.join(2)
            if thread.is_alive():
                raise RuntimeError('owned cleanup thread still alive: ' + thread.name)

    def cleanup(self):
        if getattr(self, 'cleanup_complete', False):
            return
        errors = []
        def release(label, operation):
            try:
                operation()
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append((label, exc))
        handles = list(getattr(self, 'all_handles', ()))
        if not self.closed:
            for handle in handles:
                if not handle.done.is_set():
                    release('cancel ' + handle.identifier,
                            lambda h=handle: self.cleanup_receipts.append(self.signal_owned(h)))
        release('launcher', self._stop_launcher)
        self.closed = True  # Reject new work, without declaring all releases complete.
        for handle in handles:
            with handle._condition:
                handle._condition.notify_all()
            release('pump ' + handle.identifier, lambda h=handle: self._join_started(h._pump))
        release('demultiplexer', lambda: self._join_started(getattr(self, 'reader', None)))
        proc = getattr(self, 'proc', None)
        streams = [getattr(self, 'stderr_file', None)]
        if proc is not None:
            streams = [proc.stdout, proc.stdin, *streams]
        for number, stream in enumerate(streams):
            if stream is not None and not stream.closed:
                release('stream ' + str(number), stream.close)
        self.cleanup_errors.extend({'resource': label, 'error': repr(exc)} for label, exc in errors)
        self.cleanup_complete = not errors
        if errors:
            error = RuntimeError('owned cleanup not fully verified')
            for label, exc in errors:
                error.add_note(label + ': ' + repr(exc))
            raise error from errors[0][1]
