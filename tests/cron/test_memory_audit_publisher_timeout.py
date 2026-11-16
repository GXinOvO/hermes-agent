import hashlib
from contextlib import contextmanager
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cron import memory_audit_policy
from cron.memory_audit_policy import RunPolicy
from tests.agent import test_tool_call_guardrail_runtime as fixtures
from tests.cron.test_memory_audit_tool_scope import bind_report_store, invoke
from tests.waiting import wait_until


@pytest.fixture
def publisher_call(tmp_path, monkeypatch):
    source = tmp_path / 'report.md'
    source.write_text('report', encoding='utf-8')
    marker = tmp_path / 'published'
    publisher = tmp_path / 'publisher.py'
    publisher.write_text(
        f'import pathlib\npathlib.Path({str(marker)!r}).write_bytes(__memory_audit_source_bytes__)\n'
        'print("{}")\n', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    agent = fixtures._make_agent('terminal', platform='cron')
    agent._memory_audit_policy = policy
    command = shlex.join([sys.executable, str(publisher), '--source', str(source), '--run-time', policy.run_time])
    return agent, command, marker


@pytest.mark.parametrize(('requested', 'expected'), [(None, 180), (10, 10), (600, 180)])
def test_publisher_accepts_timeout_without_exceeding_audit_limit(publisher_call, monkeypatch, requested, expected):
    agent, command, marker = publisher_call
    real_popen = subprocess.Popen
    observed = []

    class CapturingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            self.is_publisher = str(agent._memory_audit_policy.publisher) in args[0]
            super().__init__(*args, **kwargs)

        def communicate(self, *args, **kwargs):
            if self.is_publisher:
                observed.append(kwargs.get('timeout'))
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', CapturingPopen)
    call_args = {'command': command} if requested is None else {'command': command, 'timeout': requested}
    result = invoke(agent, 'terminal', call_args, 'publish-timeout')
    assert result.get('exit_code') == 0, result
    assert marker.read_text(encoding='utf-8') == 'report'
    assert observed == [expected]


def test_publisher_stops_when_requested_timeout_expires(publisher_call):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    policy.publisher.write_text(
        f'import pathlib,time\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text("late")\n',
        encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 1}, 'publish-timed-out')
    assert 'timed out' in result.get('error', ''), result
    assert policy.publication_receipt is None
    assert not marker.exists()


def test_publisher_fails_closed_when_namespace_launcher_identity_changes(publisher_call, monkeypatch):
    agent, command, marker = publisher_call
    monkeypatch.setattr(memory_audit_policy, 'UNSHARE_SHA256', '0' * 64)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 600}, 'invalid-namespace-launcher')
    assert result.get('error') == 'memory_audit_publisher_supervisor_unavailable'
    assert not marker.exists()
    assert agent._memory_audit_policy.publication_receipt is None


def test_publisher_drops_inherited_python_startup_hooks(publisher_call, monkeypatch, tmp_path):
    agent, command, marker = publisher_call
    hook_marker = tmp_path / 'sitecustomize-ran'
    (tmp_path / 'sitecustomize.py').write_text(
        f'import json,os,pathlib,sys\n'
        f'nspid=[line for line in open("/proc/self/status") if line.startswith("NSpid:")]\n'
        f'if "-c" in sys.argv and nspid and nspid[0].split()[-1] == "1": pathlib.Path({str(hook_marker)!r}).write_text(json.dumps({{"pid": os.getpid(), "argv": sys.argv, "path": sys.path, "cwd": os.getcwd()}}))\n',
        encoding='utf-8')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    observed = []
    original_popen = memory_audit_policy.subprocess.Popen

    def capture_popen(argv, **kwargs):
        observed.append(argv)
        return original_popen(argv, **kwargs)

    monkeypatch.setattr(memory_audit_policy.subprocess, 'Popen', capture_popen)
    result = json.loads(agent._memory_audit_policy.run_report_action(
        'publish_report', {'command': command, 'timeout': 600}))
    assert result.get('exit_code') == 0, result
    assert marker.read_text(encoding='utf-8') == 'report'
    assert not hook_marker.exists()
    assert any('-B' in argv for argv in observed)


@pytest.mark.parametrize('failure', ['pidfd_rights', 'credentials'])
def test_publisher_does_not_start_before_supervisor_is_bound(publisher_call, monkeypatch, failure):
    agent, command, marker = publisher_call
    if failure == 'pidfd_rights':
        recvmsg = memory_audit_policy.socket.socket.recvmsg

        def strip_pidfd(self, *args, **kwargs):
            data, ancillary, flags, address = recvmsg(self, *args, **kwargs)
            return data, [item for item in ancillary if item[1] != memory_audit_policy.socket.SCM_RIGHTS], flags, address

        monkeypatch.setattr(memory_audit_policy.socket.socket, 'recvmsg', strip_pidfd)
    else:
        monkeypatch.setattr(memory_audit_policy.os, 'geteuid', lambda: -1)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, f'unbound-supervisor-{failure}')
    assert result.get('error'), result
    assert not marker.exists()
    assert agent._memory_audit_policy.publication_receipt is None


def test_publisher_fails_closed_when_socket_setup_fails(publisher_call, monkeypatch):
    agent, command, marker = publisher_call

    def fail_socketpair(*args, **kwargs):
        raise OSError('injected socketpair failure')

    monkeypatch.setattr(memory_audit_policy.socket, 'socketpair', fail_socketpair)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, 'socketpair-failure')
    assert result.get('error'), result
    assert not marker.exists()
    assert agent._memory_audit_policy.publication_receipt is None


def test_publisher_retries_interrupted_pidfd_wait(publisher_call, monkeypatch):
    agent, command, marker = publisher_call
    original_poll = memory_audit_policy.select.poll
    interrupts = []

    class InterruptOnce:
        def __init__(self):
            self._poll = original_poll()
            self._interrupted = False

        def register(self, *args, **kwargs):
            return self._poll.register(*args, **kwargs)

        def poll(self, *args, **kwargs):
            if not self._interrupted:
                self._interrupted = True
                interrupts.append(True)
                raise InterruptedError('injected EINTR')
            return self._poll.poll(*args, **kwargs)

    monkeypatch.setattr(memory_audit_policy.select, 'poll', InterruptOnce)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 600}, 'pidfd-eintr')
    assert result.get('exit_code') == 0, result
    assert interrupts == [True]
    assert marker.read_text(encoding='utf-8') == 'report'


@pytest.mark.parametrize('ready', [True, False])
@pytest.mark.parametrize('kill_fails', [False, True])
def test_supervisor_wait_returns_a_bounded_confirmation(monkeypatch, ready, kill_fails):
    from types import SimpleNamespace

    ticks = iter(range(100))
    polls, kills = [], []

    class Poll:
        def register(self, fd, mask):
            assert fd == 42

        def poll(self, milliseconds):
            polls.append(milliseconds)
            assert len(polls) <= 12, 'supervisor wait became unbounded'
            return [(42, memory_audit_policy.select.POLLIN)] if ready else []

    def kill(fd, sig):
        assert fd == 42 and sig == signal.SIGKILL
        kills.append(True)
        if kill_fails:
            raise PermissionError('injected supervisor kill failure')

    monkeypatch.setattr(memory_audit_policy.select, 'poll', Poll)
    monkeypatch.setattr(memory_audit_policy.time, 'monotonic', lambda: next(ticks) * 0.5)
    monkeypatch.setattr(memory_audit_policy.signal, 'pidfd_send_signal', kill)
    proc = SimpleNamespace(poll=lambda: None, kill=lambda: pytest.fail('numeric PID kill must not replace pidfd'))
    assert memory_audit_policy._wait_supervisor(42, proc, 1) is ready
    assert bool(kills) is not ready


def test_unconfirmed_teardown_cannot_return_success(publisher_call, monkeypatch):
    agent, command, marker = publisher_call
    confirmations = []

    def unconfirmed(*args):
        confirmations.append(True)
        return False

    monkeypatch.setattr(memory_audit_policy, '_wait_supervisor', unconfirmed)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 600}, 'teardown-unconfirmed')
    assert confirmations == [True]
    assert result.get('error') == 'memory_audit_publisher_teardown_unconfirmed', result
    assert agent._memory_audit_policy.publication_receipt is None
    assert marker.read_text(encoding='utf-8') == 'report'


def test_guardian_failure_prevents_publisher_start(publisher_call, monkeypatch):
    agent, command, marker = publisher_call
    launches = []
    real_popen = subprocess.Popen

    def fail_guardian(argv, **kwargs):
        if memory_audit_policy._LOCK_GUARDIAN_BOOTSTRAP in argv:
            launches.append('guardian')
            raise OSError('injected guardian launch failure')
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', fail_guardian)
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 1}, 'guardian-failed')
    assert launches == ['guardian']
    assert result.get('error') == 'memory_audit_publisher_lock_guardian_unavailable', result
    assert not marker.exists()
    assert agent._memory_audit_policy.publication_receipt is None


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('confirm_exit', [False, True])
def test_failed_kill_returns_error_while_guardian_retains_lock(publisher_call, monkeypatch, confirm_exit):
    import fcntl

    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('failed-kill-started')
    release = marker.with_name('failed-kill-release')
    policy.publisher.write_text(
        'import os,pathlib,time\n'
        f'pathlib.Path({str(started)!r}).write_text("ready")\n'
        f'while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    real_popen = subprocess.Popen
    real_receive = memory_audit_policy._receive_supervisor
    observed = []
    supervisor_fds = []

    def receive(*args):
        fd = real_receive(*args)
        supervisor_fds.append(os.dup(fd))
        return fd

    class FailedKillPopen(real_popen):
        def __init__(self, argv, **kwargs):
            self.is_publisher = str(policy.publisher) in argv
            super().__init__(argv, **kwargs)

        def communicate(self, *args, **kwargs):
            if not self.is_publisher:
                return super().communicate(*args, **kwargs)
            assert self.stdin is not None
            self.stdin.write(kwargs['input'])
            self.stdin.flush()
            self.stdin.close()
            self.stdin = None
            wait_until(lambda: started.exists() and started.read_text() == 'ready', timeout=5)
            signal.pidfd_send_signal(supervisor_fds[0], signal.SIGSTOP)

            def supervisor_stopped():
                info = Path(f'/proc/self/fdinfo/{supervisor_fds[0]}').read_text()
                pid = next(int(line.split(':', 1)[1]) for line in info.splitlines() if line.startswith('Pid:'))
                tasks = list(Path(f'/proc/{pid}/task').iterdir())
                return tasks and all(next(line.split()[1] for line in (task / 'status').read_text().splitlines()
                                          if line.startswith('State:')) == 'T' for task in tasks)

            wait_until(supervisor_stopped, timeout=5, what='namespace threads acknowledge SIGSTOP before owner EOF')
            observed.append(self)
            raise subprocess.TimeoutExpired(self.args, 1)

        def kill(self):
            if self.is_publisher:
                raise PermissionError('injected kill refusal')
            return super().kill()

    monkeypatch.setattr(subprocess, 'Popen', FailedKillPopen)
    monkeypatch.setattr(memory_audit_policy, '_receive_supervisor', receive)
    if not confirm_exit:
        monkeypatch.setattr(memory_audit_policy, '_wait_supervisor', lambda *args: False)
    lock_path = policy.audit_lock_path.with_suffix(policy.audit_lock_path.suffix + '.lock')
    try:
        result = invoke(agent, 'terminal', {'command': command, 'timeout': 1}, 'failed-kill')
        assert len(observed) == 1
        assert policy.publication_receipt is None
        if confirm_exit:
            poller = memory_audit_policy.select.poll()
            poller.register(supervisor_fds[0], memory_audit_policy.select.POLLIN)
            assert any(fd == supervisor_fds[0] and mask & memory_audit_policy.select.POLLIN
                       for fd, mask in poller.poll(0)), result
            assert 'timed out' in result.get('error', ''), result
        else:
            assert result.get('error') == 'memory_audit_publisher_teardown_unconfirmed', result
            with lock_path.open('r+') as outsider:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
            signal.pidfd_send_signal(supervisor_fds[0], signal.SIGKILL)
        observed[0].wait(timeout=5)

        def lock_released():
            with lock_path.open('r+') as outsider:
                try:
                    fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False

        wait_until(lock_released, timeout=5, what='failed-kill guardian releases after namespace exit')
    finally:
        release.touch()
        for fd in supervisor_fds:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(fd)
        for proc in observed:
            proc.wait(timeout=5)


def test_publisher_holds_audit_lock_until_namespace_exit(publisher_call):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    lock_path = policy.audit_lock_path.with_suffix(policy.audit_lock_path.suffix + '.lock')
    witness = marker.with_name('lock-witness')
    child_code = (
        "import fcntl,pathlib,time\n"
        f"fd=open({str(lock_path)!r}, 'r+')\n"
        "try:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); state='acquired'\n"
        "except BlockingIOError:\n state='blocked'\n"
        f"pathlib.Path({str(witness)!r}).write_text(state)\n"
        "time.sleep(20)\n"
    )
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'code={child_code!r}\n'
        'subprocess.Popen([sys.executable,"-c",code], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'while not pathlib.Path({str(witness)!r}).exists(): time.sleep(0.01)\n'
        'time.sleep(20)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 5}, 'lock-handoff')
    assert 'timed out' in result.get('error', ''), result
    assert witness.read_text(encoding='utf-8') == 'blocked'


@pytest.mark.parametrize(('new_session', 'publisher_exits', 'close_pipes'), [
    (False, False, False), (True, False, False), (False, True, False),
    (True, True, False), (True, True, True),
])
@pytest.mark.platforms('linux')
def test_publisher_timeout_stops_catalog_child(publisher_call, new_session, publisher_exits, close_pipes):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('child-started')
    release = marker.with_name('release-child')
    child = (f'import os,pathlib,time; {"os.setsid(); " if new_session else ""}'
             f'{"os.close(1); os.close(2); " if close_pipes else ""}'
             'fields=pathlib.Path("/proc/self/stat").read_text().rsplit(") ",1)[1].split(); '
             f'pathlib.Path({str(started)!r}).write_text(pathlib.Path("/proc/self").resolve().name+" "+fields[19]); '
             f'gate=pathlib.Path({str(release)!r}); '
             'deadline=time.monotonic()+20; '
             'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\\n"); '
             f'pathlib.Path({str(marker)!r}).write_text("late")')
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable, "-c", {child!r}], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'while not pathlib.Path({str(started)!r}).exists(): time.sleep(0.01)\n'
        f'{"sys.exit(0)" if publisher_exits else "time.sleep(10)"}\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, 'publish-child-timeout')
    assert 'timed out' in result.get('error', ''), result
    assert started.exists()
    pid, start_ticks = started.read_text().split()
    stat_path = Path(f'/proc/{pid}/stat')

    def same_process_running():
        try:
            fields = stat_path.read_text().rsplit(') ', 1)[1].split()
        except FileNotFoundError:
            return False
        return fields[19] == start_ticks and fields[0] != 'Z'

    try:
        wait_until(lambda: not same_process_running(), timeout=3, what='publisher descendant exit')
        release.touch()
        assert not marker.exists()
    finally:
        if same_process_running():
            release.touch()
            wait_until(lambda: not same_process_running(), timeout=3, what='test child cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('incomplete_snapshot', [False, True])
def test_timeout_descendant_is_gone_before_return(publisher_call, monkeypatch, incomplete_snapshot):
    from agent import deadline

    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('fallback-child-started')
    release = marker.with_name('fallback-child-release')
    child = (
        'import os,pathlib,time; os.setsid(); os.close(1); os.close(2); '
        'fields=pathlib.Path("/proc/self/stat").read_text().rsplit(") ",1)[1].split(); '
        f'pathlib.Path({str(started)!r}).write_text(pathlib.Path("/proc/self").resolve().name+" "+fields[19]); '
        f'gate=pathlib.Path({str(release)!r}); deadline=time.monotonic()+30; '
        'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\\n"); '
        f'pathlib.Path({str(marker)!r}).write_text("late")'
    )
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable,"-c",{child!r}], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'while not pathlib.Path({str(started)!r}).exists(): time.sleep(0.01)\n'
        'time.sleep(30)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    injected = []
    if incomplete_snapshot:
        @contextmanager
        def empty_snapshot(pid, *, hard_kill):
            injected.append((pid, hard_kill))
            yield []
        monkeypatch.setattr(deadline, '_process_tree_snapshot', empty_snapshot)

    result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, 'publish-incomplete-snapshot')
    assert 'timed out' in result.get('error', ''), result
    assert started.exists()
    pid, start_ticks = started.read_text().split()
    stat_path = Path(f'/proc/{pid}/stat')

    def same_process_running():
        try:
            fields = stat_path.read_text().rsplit(') ', 1)[1].split()
        except FileNotFoundError:
            return False
        return fields[19] == start_ticks and fields[0] != 'Z'

    try:
        assert not same_process_running(), 'publisher descendant survived when locks were released'
        assert not injected
        assert not marker.exists()
    finally:
        if same_process_running():
            release.touch()
            wait_until(lambda: not same_process_running(), timeout=3, what='injected test child cleanup')


@pytest.mark.platforms('linux')
def test_supervisor_death_does_not_leave_publisher_descendant(publisher_call, monkeypatch):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('supervisor-death-started')
    release = marker.with_name('supervisor-death-release')
    child = (
        'import os,pathlib,time; os.setsid(); os.close(1); os.close(2); '
        'fields=pathlib.Path("/proc/self/stat").read_text().rsplit(") ",1)[1].split(); '
        f'pathlib.Path({str(started)!r}).write_text(pathlib.Path("/proc/self").resolve().name+" "+fields[19]); '
        f'gate=pathlib.Path({str(release)!r}); deadline=time.monotonic()+20; '
        'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\\n"); '
        f'pathlib.Path({str(marker)!r}).write_text("late")'
    )
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable,"-c",{child!r}], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'gate=pathlib.Path({str(release)!r})\n'
        'deadline=time.monotonic()+20\n'
        'while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    original_popen = subprocess.Popen
    injected, watchers = [], []

    def launch_and_interrupt(argv, **kwargs):
        proc = original_popen(argv, **kwargs)
        if str(policy.publisher) in argv:
            def interrupt():
                wait_until(lambda: started.exists() and started.read_text().strip(), timeout=5)
                proc.kill()
                injected.append(proc.pid)
            watcher = threading.Thread(target=interrupt)
            watchers.append(watcher)
            watcher.start()
        return proc

    monkeypatch.setattr(subprocess, 'Popen', launch_and_interrupt)
    try:
        result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, 'supervisor-killed')
        for watcher in watchers:
            watcher.join(5)
        assert len(injected) == 1
        assert result.get('exit_code') != 0
        pid, start_ticks = started.read_text().split()
        stat_path = Path(f'/proc/{pid}/stat')

        def same_process_running():
            try:
                fields = stat_path.read_text().rsplit(') ', 1)[1].split()
            except FileNotFoundError:
                return False
            return fields[19] == start_ticks and fields[0] != 'Z'

        assert not same_process_running(), 'publisher descendant survived supervisor death and lock release'
    finally:
        release.touch()
        for watcher in watchers:
            watcher.join(5)
        if started.exists() and started.read_text().strip():
            wait_until(lambda: not same_process_running(), timeout=3, what='supervisor-death test cleanup')


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('phase', ['before_ready', 'ready_before_go'])
def test_owner_death_before_start_releases_namespace_locks(publisher_call, monkeypatch, phase):
    import fcntl
    import select
    import socket
    import struct

    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    reached = marker.with_name('owner-start-phase')
    release = marker.with_name('owner-start-release')
    real_popen = subprocess.Popen
    real_receive = memory_audit_policy._receive_supervisor
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)

    def receive(control, proc, pidfd):
        fd = real_receive(control, proc, pidfd)
        child.sendmsg([b'BOUND'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('i', fd))])
        if phase == 'ready_before_go':
            reached.write_text('ready_before_go')
            child.recv(1)
        return fd

    def pause_before_ready(argv, **kwargs):
        if str(policy.publisher) in argv and phase == 'before_ready':
            argv = list(argv)
            index = argv.index(memory_audit_policy._SUPERVISED_PUBLISHER_BOOTSTRAP)
            anchor = "control.sendmsg([b'READY']"
            assert argv[index].count(anchor) == 1
            pause = ('import pathlib,time\n'
                     f'pathlib.Path({str(reached)!r}).write_text("before_ready")\n'
                     f'while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n')
            argv[index] = argv[index].replace(anchor, pause + anchor)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', pause_before_ready)
    monkeypatch.setattr(memory_audit_policy, '_receive_supervisor', receive)
    owner = os.fork()
    if owner == 0:
        parent.close()
        try:
            invoke(agent, 'terminal', {'command': command, 'timeout': 10}, 'owner-before-go')
        finally:
            os._exit(0)
    child.close()
    namespace_fd = None
    try:
        wait_until(lambda: reached.exists() and reached.read_text() == phase, timeout=5)
        if phase == 'ready_before_go':
            payload, ancillary, flags, _ = parent.recvmsg(16, socket.CMSG_SPACE(struct.calcsize('i')))
            assert payload == b'BOUND' and flags == 0
            rights = [value for level, kind, value in ancillary
                      if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS]
            assert len(rights) == 1
            namespace_fd = struct.unpack('i', rights[0])[0]
        os.kill(owner, signal.SIGKILL)
        _, status = os.waitpid(owner, 0)
        assert os.WIFSIGNALED(status)
        release.touch()
        if namespace_fd is not None:
            poller = select.poll()
            poller.register(namespace_fd, select.POLLIN)
            assert any(fd == namespace_fd and events & select.POLLIN for fd, events in poller.poll(5000))
        lock_path = policy.audit_lock_path.with_suffix(policy.audit_lock_path.suffix + '.lock')

        def lock_released():
            with lock_path.open('r+') as outsider:
                try:
                    fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False

        wait_until(lock_released, timeout=5, what='pre-start namespace lock release')
        assert not marker.exists()
    finally:
        release.touch()
        parent.close()
        if namespace_fd is not None:
            os.close(namespace_fd)
        try:
            waited, _ = os.waitpid(owner, os.WNOHANG)
            if waited == 0:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
        except ChildProcessError:
            pass


@pytest.mark.platforms('linux')
def test_owner_death_does_not_leave_publisher_descendant(publisher_call):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('owner-death-started')
    release = marker.with_name('owner-death-release')
    child = (
        'import os,pathlib,time; os.setsid(); os.close(1); os.close(2); '
        'fields=pathlib.Path("/proc/self/stat").read_text().rsplit(") ",1)[1].split(); '
        f'pathlib.Path({str(started)!r}).write_text(pathlib.Path("/proc/self").resolve().name+" "+fields[19]); '
        f'gate=pathlib.Path({str(release)!r}); deadline=time.monotonic()+20; '
        'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\\n"); '
        f'pathlib.Path({str(marker)!r}).write_text("late")'
    )
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable,"-c",{child!r}], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'while not pathlib.Path({str(started)!r}).exists(): time.sleep(0.01)\n'
        'time.sleep(30)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    owner = os.fork()
    if owner == 0:
        try:
            invoke(agent, 'terminal', {'command': command, 'timeout': 600}, 'owner-death')
        finally:
            os._exit(0)
    try:
        wait_until(lambda: started.exists(), timeout=5, what='owner-death child start')
        os.kill(owner, signal.SIGKILL)
        _, status = os.waitpid(owner, 0)
        assert os.WIFSIGNALED(status)
        pid, start_ticks = started.read_text().split()
        stat_path = Path(f'/proc/{pid}/stat')

        def same_process_running():
            try:
                fields = stat_path.read_text().rsplit(') ', 1)[1].split()
            except FileNotFoundError:
                return False
            return fields[19] == start_ticks and fields[0] != 'Z'

        wait_until(lambda: not same_process_running(), timeout=3, what='owner-death descendant exit')
        assert not marker.exists()
    finally:
        release.touch()
        try:
            waited, _ = os.waitpid(owner, os.WNOHANG)
            if waited == 0:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
        except ChildProcessError:
            pass


@pytest.mark.platforms('linux')
def test_namespace_cleanup_kills_detached_descendant(publisher_call):
    agent, command, marker = publisher_call
    policy = agent._memory_audit_policy
    started = marker.with_name('signal-refused-started')
    release = marker.with_name('signal-refused-release')
    child = (
        'import os,pathlib,time; os.setsid(); os.close(1); os.close(2); '
        'fields=pathlib.Path("/proc/self/stat").read_text().rsplit(") ",1)[1].split(); '
        f'pathlib.Path({str(started)!r}).write_text(pathlib.Path("/proc/self").resolve().name+" "+fields[19]); '
        f'gate=pathlib.Path({str(release)!r}); deadline=time.monotonic()+20; '
        'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\\n"); '
        f'pathlib.Path({str(marker)!r}).write_text("late")'
    )
    policy.publisher.write_text(
        'import pathlib,subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable,"-c",{child!r}], stdin=subprocess.DEVNULL, '
        'pass_fds=(__memory_audit_interpreter_fd__,))\n'
        f'while not pathlib.Path({str(started)!r}).exists(): time.sleep(0.01)\n'
        'time.sleep(20)\n', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    try:
        result = invoke(agent, 'terminal', {'command': command, 'timeout': 2}, 'namespace-cleanup')
        assert result.get('error'), result
        pid, start_ticks = started.read_text().split()
        stat_path = Path(f'/proc/{pid}/stat')

        def same_process_running():
            try:
                fields = stat_path.read_text().rsplit(') ', 1)[1].split()
            except FileNotFoundError:
                return False
            return fields[19] == start_ticks and fields[0] != 'Z'

        assert not same_process_running(), 'signal failure released locks with a live publisher child'
    finally:
        release.touch()
        if started.exists() and started.read_text().strip():
            wait_until(lambda: not same_process_running(), timeout=3, what='signal-refusal test cleanup')


@pytest.mark.parametrize('extra', [
    {'timeout': 0}, {'timeout': -1}, {'timeout': True}, {'timeout': '600'},
    {'timeout': 1.5}, {'timeout': None}, {'timeout': 601},
    {'timeout': 600, 'background': True}, {'timeout': 600, 'workdir': '/'},
])
def test_publisher_rejects_invalid_terminal_arguments(publisher_call, extra):
    agent, command, marker = publisher_call
    result = invoke(agent, 'terminal', {'command': command, **extra}, 'publish-invalid')
    assert result.get('error') == 'memory_audit_tool_scope_blocked', result
    assert not marker.exists()


def test_publisher_timeout_does_not_allow_command_injection(publisher_call, tmp_path):
    agent, command, marker = publisher_call
    injected = tmp_path / 'injected'
    command += ' ; touch ' + shlex.quote(str(injected))
    result = invoke(agent, 'terminal', {'command': command, 'timeout': 600}, 'publish-injected')
    assert result.get('error') == 'memory_audit_tool_scope_blocked', result
    assert not marker.exists()
    assert not injected.exists()