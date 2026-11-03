"""Real Git index-lock lifecycle through the public checkpoint write preflight."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import stat
import subprocess
import sys
from threading import Event

import pytest

from tests.waiting import wait_until
from tools import checkpoint_manager as checkpoints
from tools import checkpoint_maintenance


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", base)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.txt").write_bytes(b"original\x00bytes\n")
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    assert manager.ensure_checkpoint(str(root), "initial")
    return root, base, manager


@pytest.fixture
def stalled_add(project, tmp_path):
    """The real git add owns its lock before the clean filter reports ready."""
    root, base, _manager = project
    store = checkpoints._store_path(base)
    index = checkpoints._index_path(store, checkpoints._project_hash(str(root)))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(15)
    script = tmp_path / "hold_filter.py"
    script.write_text('''import json, os, socket, sys
from pathlib import Path
try:
    descriptor = os.open(sys.argv[2], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    raise SystemExit(0)
os.close(descriptor)
connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=15)
lock = Path(os.environ["GIT_INDEX_FILE"] + ".lock")
connection.sendall((json.dumps({"pid": os.getpid(), "git_pid": os.getppid(),
                              "lock_exists": lock.exists()}) + "\\n").encode())
connection.settimeout(30)
connection.recv(1)
sys.stdout.buffer.write(sys.stdin.buffer.read())
''', encoding="utf-8")
    command = shlex.join([sys.executable, str(script), str(listener.getsockname()[1]), str(tmp_path / "filter-entered")])
    ok, _out, error = checkpoints._run_git(["config", "filter.hold.clean", command], store, str(root))
    assert ok, error
    ok, _out, error = checkpoints._run_git(["config", "filter.hold.required", "true"], store, str(root))
    assert ok, error
    (root / ".gitattributes").write_text("main.txt filter=hold\n", encoding="utf-8")
    (root / "main.txt").write_bytes(b"pending snapshot\n")
    try:
        yield listener, index.with_name(index.name + ".lock")
    finally:
        listener.close()
        ok, _out, error = checkpoints._run_git(["config", "--unset", "filter.hold.clean"], store, str(root))
        assert ok, error
        (root / ".gitattributes").unlink(missing_ok=True)


@pytest.mark.platforms("posix")
def test_add_timeout_releases_its_real_git_index_lock(project, stalled_add, monkeypatch):
    root, _base, manager = project
    listener, lock = stalled_add
    monkeypatch.setattr(checkpoints, "_GIT_TIMEOUT", 2)
    manager.new_turn()
    connection = None
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(manager.ensure_checkpoint, str(root), "timeout")
        try:
            connection, _address = listener.accept()
            with connection.makefile("rb") as reader:
                ready = json.loads(reader.readline())
            assert ready["lock_exists"], "the real Git did not reach its index transaction"
            assert lock.exists()
            assert future.result(timeout=15) is False
            assert not lock.exists(), "timed-out git add stranded its per-project index.lock"
            assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]
        finally:
            if connection is not None:
                try:
                    connection.sendall(b"x")
                except OSError:
                    pass
                connection.close()
    # A later write preflight must produce a usable recovery point.
    (root / ".gitattributes").unlink()
    manager.new_turn()
    assert manager.ensure_checkpoint(str(root), "retry after timeout")


def _ready(listener):
    connection, _address = listener.accept()
    connection.settimeout(15)
    with connection.makefile("rb") as reader:
        state = json.loads(reader.readline())
    assert state["lock_exists"]
    return connection, state


def _release(connection):
    try:
        connection.sendall(b"x")
    except OSError:
        pass  # a successfully terminated filter has already closed this socket
    connection.close()


def _exited(pid):
    import psutil

    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.platforms("posix")
def test_timeout_terminates_filter_before_returning(project, stalled_add, monkeypatch):
    root, _base, manager = project
    listener, lock = stalled_add
    monkeypatch.setattr(checkpoints, "_GIT_TIMEOUT", 2)
    manager.new_turn()
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(manager.ensure_checkpoint, str(root), "timeout with descendant")
        connection, state = _ready(listener)
        try:
            assert future.result(timeout=15) is False
            wait_until(lambda: _exited(state["pid"]), timeout=5, what="Git's filter to exit")
            assert not lock.exists()
        finally:
            _release(connection)


@pytest.mark.platforms("posix")
def test_live_git_lock_survives_contenders_until_publish(project, stalled_add):
    root, base, manager = project
    listener, lock = stalled_add
    manager.new_turn()
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(manager.ensure_checkpoint, str(root), "held snapshot")
        connection, _state = _ready(listener)
        identity = lock.stat()
        try:
            contender = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
            assert contender.ensure_checkpoint(str(root), "contender") is False
            assert checkpoint_maintenance.prune_checkpoints(
                retention_days=0, checkpoint_base=base,
            )["errors"] > 0
            assert not checkpoint_maintenance.clear_all(base)["deleted"]
            assert lock.stat().st_ino == identity.st_ino
            assert not future.done(), "snapshot preflight returned before Git published its tree"
        finally:
            _release(connection)
        assert future.result(timeout=15)
    assert not lock.exists()
    assert manager.list_checkpoints(str(root))[0]["reason"] == "held snapshot"


@pytest.mark.platforms("posix")
def test_foreign_index_lock_is_preserved_without_claiming_a_checkpoint(project):
    root, base, manager = project
    index = checkpoints._index_path(checkpoints._store_path(base), checkpoints._project_hash(str(root)))
    lock = index.with_name(index.name + ".lock")
    lock.write_bytes(b"foreign writer transaction")
    os.utime(lock, (1, 1))  # age must never authorize deletion
    identity = lock.stat()
    (root / "main.txt").write_bytes(b"not backed up yet")
    before = manager.list_checkpoints(str(root))
    manager.new_turn()
    assert manager.ensure_checkpoint(str(root), "blocked by foreign lock") is False
    assert lock.read_bytes() == b"foreign writer transaction"
    assert lock.stat().st_ino == identity.st_ino
    assert manager.list_checkpoints(str(root)) == before
    assert (root / "main.txt").read_bytes() == b"not backed up yet"


@pytest.mark.platforms("posix")
def test_nonzero_git_exit_does_not_publish_a_checkpoint(project, stalled_add):
    root, _base, manager = project
    listener, lock = stalled_add
    manager.new_turn()
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(manager.ensure_checkpoint, str(root), "filter failure")
        connection, state = _ready(listener)
        try:
            os.kill(state["pid"], signal.SIGTERM)
            assert future.result(timeout=15) is False
            assert not lock.exists()
            assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]
        finally:
            _release(connection)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("drain_fails", [False, True])
def test_keyboard_interrupt_in_git_wait_cleans_up_and_propagates(project, stalled_add, monkeypatch, drain_fails):
    """Real Git + injected wait/drain faults; caller stop-event delivery is out of scope."""
    root, _base, manager = project
    listener, lock = stalled_add
    interrupted = Event()
    communicate = subprocess.Popen.communicate
    first = True

    def interrupt_wait(process, *args, **kwargs):
        nonlocal first
        if first:
            first = False
            assert interrupted.wait(timeout=15)
            raise KeyboardInterrupt("controlled checkpoint wait interrupt")
        if drain_fails:
            raise OSError("controlled pipe drain failure")
        return communicate(process, *args, **kwargs)

    store = checkpoints._store_path()
    index = checkpoints._index_path(store, checkpoints._project_hash(str(root)))
    with ThreadPoolExecutor(max_workers=1) as worker:
        with monkeypatch.context() as patcher:
            patcher.setattr(subprocess.Popen, "communicate", interrupt_wait)
            future = worker.submit(checkpoints._run_git, ["add", "-A"], store, str(root), index_file=index)
            connection, state = _ready(listener)
            try:
                interrupted.set()
                with pytest.raises(KeyboardInterrupt, match="controlled checkpoint wait interrupt"):
                    future.result(timeout=15)
                assert not lock.exists()
                wait_until(lambda: _exited(state["pid"]), timeout=5, what="interrupted filter to exit")
            finally:
                interrupted.set()
                _release(connection)
    assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]


@pytest.mark.platforms("posix")
def test_snapshot_restore_recovers_uncommitted_bytes_and_executable_mode(project):
    root, base, manager = project
    source = root / "main.txt"
    expected = b"uncommitted before write\x00\xff\n"
    source.write_bytes(expected)
    source.chmod(0o755)
    link = root / "source-link"
    link.symlink_to("main.txt")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(root), "write precondition")
    target = manager.list_checkpoints(str(root))[0]["hash"]
    source.write_bytes(b"after agent write\n")
    source.chmod(0o644)
    link.unlink()
    link.symlink_to("changed-target")
    result = manager.restore(str(root), target)
    assert result["success"], result
    assert source.read_bytes() == expected
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    assert os.readlink(link) == "main.txt"
    # A synchronous pre-rollback snapshot makes the overwritten content recoverable too.
    safety = manager.list_checkpoints(str(root))[0]
    assert safety["reason"].startswith("pre-rollback snapshot")
    ok, output, error = checkpoints._run_git(
        ["show", safety["hash"] + ":main.txt"], checkpoints._store_path(base), str(root),
    )
    assert ok, error
    assert output == "after agent write"


@pytest.mark.platforms("posix")
def test_timeout_escalates_for_a_launcher_ignoring_termination(project, stalled_add, tmp_path):
    """A real Git child cleans its lock while its uncooperative launcher needs SIGKILL."""
    import shutil

    root, base, manager = project
    listener, lock = stalled_add
    git = shutil.which("git")
    wrapper_dir = tmp_path / "git-wrapper"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    pid_file = tmp_path / "launcher-pid"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, threading\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"child = subprocess.Popen([{git!r}, *sys.argv[1:]])\n"
        "child.wait()\n"
        "threading.Event().wait(30)\n", encoding="utf-8",
    )
    wrapper.chmod(0o755)
    store = checkpoints._store_path(base)
    index = checkpoints._index_path(store, checkpoints._project_hash(str(root)))
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(
            checkpoints._run_git, ["add", "-A"], store, str(root), timeout=2,
            index_file=index, extra_env={"PATH": str(wrapper_dir) + os.pathsep + os.environ["PATH"]},
        )
        connection, state = _ready(listener)
        try:
            ok, _out, error = future.result(timeout=15)
            assert not ok and "timed out after 2s" in error
            assert not lock.exists()
            wait_until(lambda: _exited(int(pid_file.read_text())), timeout=5, what="launcher to exit")
            wait_until(lambda: _exited(state["pid"]), timeout=5, what="filter to exit")
        finally:
            _release(connection)
    assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]


@pytest.mark.platforms("posix")
def test_exited_launcher_does_not_leave_a_git_writer_after_timeout(project, stalled_add, tmp_path):
    """The launcher can exit while a real Git descendant still owns the index."""
    import shutil

    root, base, manager = project
    listener, lock = stalled_add
    git = shutil.which("git")
    wrapper_dir = tmp_path / "exiting-launcher"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"subprocess.Popen([{git!r}, *sys.argv[1:]])\n", encoding="utf-8",
    )
    wrapper.chmod(0o755)
    store = checkpoints._store_path(base)
    index = checkpoints._index_path(store, checkpoints._project_hash(str(root)))
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(
            checkpoints._run_git, ["add", "-A"], store, str(root), timeout=2,
            index_file=index, extra_env={"PATH": str(wrapper_dir) + os.pathsep + os.environ["PATH"]},
        )
        connection, state = _ready(listener)
        try:
            ok, _out, error = future.result(timeout=15)
            assert not ok and "timed out after 2s" in error
            assert not lock.exists()
            wait_until(lambda: _exited(state["pid"]), timeout=5, what="orphaned filter to exit")
        finally:
            _release(connection)
    assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]
