import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.memory_tool_store import MemoryStore
from cron import memory_audit_policy


@pytest.mark.platforms('linux')
def test_inherited_audit_lock_survives_owner_context_exit(tmp_path):
    import fcntl

    path = tmp_path / 'audit'
    lock_path = path.with_suffix('.lock')
    parent, child = socket.socketpair()
    keeper = None
    try:
        with MemoryStore._file_lock(path) as lock_fd:
            keeper = subprocess.Popen(
                [sys.executable, '-I', '-S', '-B', '-c',
                 'import os,socket,sys; '
                 's=socket.socket(fileno=int(sys.argv[1])); '
                 's.send(b"R"); s.recv(1); os.close(int(sys.argv[2]))',
                 str(child.fileno()), str(lock_fd)],
                pass_fds=(child.fileno(), lock_fd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child.close()
            parent.settimeout(5)
            assert parent.recv(1) == b'R'
        with lock_path.open('r+') as outsider:
            with pytest.raises(BlockingIOError):
                fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent.send(b'Q')
        _, stderr = keeper.communicate(timeout=5)
        assert keeper.returncode == 0, stderr
        with lock_path.open('r+') as outsider:
            fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        parent.close()
        child.close()
        if keeper is not None and keeper.poll() is None:
            keeper.kill()
            keeper.communicate(timeout=5)


@pytest.mark.platforms('linux')
def test_pidfd_guardian_retains_lock_after_owner_returns(tmp_path):
    import fcntl

    path = tmp_path / 'audit'
    lock_path = path.with_suffix('.lock')
    parent, child = socket.socketpair()
    watched = subprocess.Popen(
        [sys.executable, '-I', '-S', '-B', '-c',
         'import socket,sys; s=socket.socket(fileno=int(sys.argv[1])); s.recv(1)', str(child.fileno())],
        pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child.close()
    pidfd = os.pidfd_open(watched.pid)
    interpreter_fd = os.open(Path(sys.executable).resolve(), os.O_RDONLY)
    guardian = None
    try:
        with MemoryStore._file_lock(path) as lock_fd:
            assert lock_fd is not None
            guardian = memory_audit_policy._start_lock_guardian(pidfd, (lock_fd,), interpreter_fd)
            assert guardian.poll() is None
        with lock_path.open('r+') as outsider:
            with pytest.raises(BlockingIOError):
                fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent.send(b'Q')
        _, stderr = watched.communicate(timeout=5)
        assert watched.returncode == 0, stderr
        assert guardian.wait(timeout=5) == 0
        with lock_path.open('r+') as outsider:
            fcntl.flock(outsider, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        parent.close()
        os.close(pidfd)
        os.close(interpreter_fd)
        if watched.poll() is None:
            watched.kill()
            watched.communicate(timeout=5)
        if guardian is not None and guardian.poll() is None:
            guardian.kill()
            guardian.wait(timeout=5)
