"""Scratch-only native terminal provider. No discovery/install side effects."""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import uuid

from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools.environments.base import BaseEnvironment

TASK_ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = Path(__file__).resolve().parent


def fixed_dir(path):
    path = Path(path)
    path.relative_to(TASK_ROOT)
    if path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise ValueError('missing or redirected task-owned directory')
    return path


def launch_argv(project, state, network):
    if network not in ('hostnet','private'):
        raise ValueError('unknown network contract')
    project, state = fixed_dir(project), fixed_dir(state)
    argv = ['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL']
    if network == 'hostnet':
        argv += ['--share-net']
    argv += ['--ro-bind','/usr','/usr']
    for name in ('bin','sbin','lib','lib64'):
        path = Path('/') / name
        if path.is_symlink():
            argv += ['--symlink',os.readlink(path),'/' + name]
        elif path.exists():
            argv += ['--ro-bind',str(path),'/' + name]
    argv += ['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir','/run',
             '--dir','/home/sandbox','--dir','/etc',
             '--bind',str(project),'/workspace','--bind',str(state),'/state',
             '--ro-bind',str(WORKER_ROOT),'/boundary']
    if network == 'hostnet':
        for path in ('/etc/hosts','/etc/resolv.conf','/etc/nsswitch.conf','/etc/ssl/certs'):
            if Path(path).exists():
                argv += ['--ro-bind',path,path]
    argv += ['--remount-ro','/','--chdir','/workspace','--clearenv',
             '--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/home/sandbox',
             '--setenv','LANG','C.UTF-8','--setenv','PYTHONDONTWRITEBYTECODE','1',
             '--','/usr/bin/python3','-I','-B','/boundary/worker.py']
    return argv


class RemoteHandle:
    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.returncode = None
        self.done = threading.Event()
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd,'r',encoding='utf-8',errors='replace')

    def output(self, raw):
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(self.write_fd,view):]
        except (BrokenPipeError,OSError):
            pass

    def finish(self, code):
        if self.done.is_set():
            return
        self.returncode = code
        os.close(self.write_fd)
        self.done.set()

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def kill(self):
        self.owner._send({'op':'cancel','id':self.identifier})

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('namespace command',timeout)
        return self.returncode


class FixedProjectEnvironment(BaseEnvironment):
    uses_host_filesystem = False
    is_local = False
    _passwordless_sudo_probe_supported = False

    def __init__(self, project, state, network='hostnet', cwd='/workspace', timeout=20):
        self.closed = False
        self.state = Path(state)
        self.state.mkdir(parents=True,exist_ok=False,mode=0o700)
        self.handles = {}
        self.handles_lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.ready = threading.Event()
        self.receipt = None
        self.argv = launch_argv(project,self.state,network)
        self.stderr_path = self.state / 'worker.stderr'
        self.stderr_file = self.stderr_path.open('w',encoding='utf-8')
        self.proc = None
        try:
            self.proc = subprocess.Popen(self.argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=self.stderr_file,text=True,encoding='utf-8',errors='replace',close_fds=True,
                cwd=TASK_ROOT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'},
                start_new_session=True)
            self.reader = threading.Thread(target=self._read,daemon=True)
            self.reader.start()
            if not self.ready.wait(10) or self.receipt is None:
                raise RuntimeError('namespace worker failed: ' + self.stderr_path.read_text(encoding='utf-8'))
            super().__init__(cwd=cwd,timeout=timeout,persist_env=True)
            self.init_session()
            if not self._snapshot_ready:
                raise RuntimeError('native session bootstrap failed inside boundary')
        except BaseException:
            self.cleanup()
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
                    handle.output(base64.b64decode(event['data'],validate=True))
                elif event.get('event') == 'exit':
                    handle.finish(event['code'])
                    with self.handles_lock:
                        self.handles.pop(event['id'],None)
        finally:
            self.ready.set()
            with self.handles_lock:
                for handle in list(self.handles.values()):
                    handle.finish(125)
                self.handles.clear()

    def _send(self, message):
        if self.closed or self.proc is None or self.proc.poll() is not None:
            raise RuntimeError('fixed namespace is closed; refusing host fallback')
        with self.send_lock:
            self.proc.stdin.write(json.dumps(message,separators=(',',':')) + '\n')
            self.proc.stdin.flush()

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        identifier = uuid.uuid4().hex
        handle = RemoteHandle(self,identifier)
        with self.handles_lock:
            self.handles[identifier] = handle
        try:
            self._send({'op':'run','id':identifier,'command':cmd_string,'stdin':stdin_data})
        except BaseException:
            with self.handles_lock:
                self.handles.pop(identifier,None)
            handle.finish(125)
            handle.stdout.close()
            raise
        return handle

    def _kill_process(self, proc):
        proc.kill()
        proc.wait(3)

    def get_temp_dir(self):
        return '/state/tmp'

    def exec_prefix(self, *, user=None, interactive=True):
        return None  # No host fallback. Browser/streams require a separate qualified path.

    def cleanup(self):
        if getattr(self,'closed',True):
            return
        self.closed = True
        proc = getattr(self,'proc',None)
        if proc is not None:
            if proc.poll() is None:
                # Killing PID1/reaper tears down every member of this PID namespace,
                # including setsid/detached descendants. Never signal arbitrary host PIDs.
                proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                raise RuntimeError('task namespace did not exit within cleanup budget')
        if hasattr(self,'stderr_file'):
            self.stderr_file.close()


class FixedProjectProvider(TerminalEnvironmentProvider):
    name = 'round7_fixed_project'
    display_name = 'Scratch fixed project'
    is_remote = False
    is_container = True
    supports_streams = False
    session_isolated_when_nonpersistent = True

    def __init__(self, project=None, lease_root=None, network='hostnet'):
        self.project = Path(project) if project is not None else TASK_ROOT / 'fixture' / 'project'
        self.lease_root = Path(lease_root) if lease_root is not None else TASK_ROOT / 'leases'
        self.network = network

    @property
    def skip_container_guards(self):
        return False

    @property
    def default_cwd(self):
        return '/workspace'

    def is_available(self):
        return Path('/usr/bin/bwrap').is_file() and self.project.is_dir()

    def create_environment(self, *, cwd, timeout, task_id='default', image=None, container_config=None, **kwargs):
        if cwd != '/workspace' and not cwd.startswith('/workspace/'):
            raise ValueError('cwd must belong to the declared in-sandbox project')
        return FixedProjectEnvironment(self.project,self.lease_root / uuid.uuid4().hex,self.network,cwd,timeout)


def register(ctx):
    ctx.register_terminal_environment_provider(FixedProjectProvider())
