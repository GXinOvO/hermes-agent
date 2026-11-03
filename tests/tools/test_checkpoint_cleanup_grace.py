"""A fast launcher must not shorten the real Git writer's TERM cleanup grace.

Linux pidfds keep STOP/CONT scoped to this test's exact Git incarnation. The
socket clean-filter fixture proves Git owns the lock before we stop it. A
controller resumes it after TERM is pending, inside the two-second grace.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event
import time
from typing import Any

import psutil
import pytest

from tests.tools import test_checkpoint_git_timeout as fixtures
from tests.waiting import wait_until
from tools import checkpoint_manager as checkpoints


# Import fixture functions, not a TestCase (which pytest would collect twice).
project = fixtures.project
stalled_add = fixtures.stalled_add


def _resume_after_term(writer, descriptor, cleanup_returned, hold_until_kill) -> dict[str, Any]:
    """Hold a real scheduling barrier, not a race to run Git's unlink handler."""
    def term_pending_or_exited() -> dict[str, Any] | None:
        if cleanup_returned.is_set():
            return {"cleanup_returned_before_resume": True}
        if fixtures._exited(writer.pid):
            return {"exited_before_resume": True}
        status = Path(f"/proc/{writer.pid}/status").read_text()
        pending = [int(line.split()[1], 16) for line in status.splitlines()
                   if line.startswith(("SigPnd:", "ShdPnd:"))]
        if any(mask & (1 << (signal.SIGTERM - 1)) for mask in pending):
            return {"term_pending_at": time.monotonic()}
        return None

    observed = wait_until(term_pending_or_exited, timeout=15, what="Git TERM pending or premature KILL")
    assert observed is not None
    if "term_pending_at" in observed and not hold_until_kill:
        # Model a bounded scheduler stall after TERM. The independent controller
        # runs even if the caller has already returned; no KILL is intercepted.
        Event().wait(0.25)  # health: allow HX013 -- delayed TERM handling is the cleanup-grace behavior under test
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
        except ProcessLookupError:
            observed["exited_before_resume"] = True
        observed["resumed_at"] = time.monotonic()
    return observed


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("mode", [
    "timeout", "timeout_reaped", "interrupt", "interrupt_drain_fails", "interrupt_closed_pipes",
    "timeout_full_grace",
])
def test_fast_launcher_preserves_git_cleanup_grace(
    project, stalled_add, tmp_path, monkeypatch, record_property, mode,
):
    root, base, manager = project
    listener, lock = stalled_add
    store = checkpoints._store_path(base)
    index = checkpoints._index_path(store, checkpoints._project_hash(str(root)))
    wrapper_dir = tmp_path / "fast-launcher"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    closed_pipes = mode == "interrupt_closed_pipes"
    redirection = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if closed_pipes else ""
    git = shutil.which("git")
    assert git is not None
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"subprocess.Popen([{git!r}, *sys.argv[1:]]{redirection})\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    armed = Event()
    cleanup_returned = Event()
    communicate = subprocess.Popen.communicate
    processes = []
    group_signals = []
    killpg = os.killpg
    original_interrupt = KeyboardInterrupt("controlled fast-launcher wait interrupt")

    def controlled_wait(process, *args, **kwargs):
        if not processes:
            processes.append(process)
            assert armed.wait(timeout=15), "real Git did not reach the scheduling barrier"
            if mode in ("timeout_reaped", "interrupt_closed_pipes"):
                process.wait(timeout=15)
            if mode.startswith("interrupt"):
                raise original_interrupt
        elif mode == "interrupt_drain_fails":
            raise OSError("controlled post-termination drain failure")
        return communicate(process, *args, **kwargs)

    def observe_group_signal(pgid, sig):
        if processes and pgid == processes[0].pid and sig != 0:
            group_signals.append((int(sig), time.monotonic()))
        return killpg(pgid, sig)

    connection = None
    descriptor = None
    observation: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=2) as workers:
        with monkeypatch.context() as patcher:
            patcher.setattr(subprocess.Popen, "communicate", controlled_wait)
            patcher.setattr(os, "killpg", observe_group_signal)
            future = workers.submit(
                checkpoints._run_git, ["add", "-A"], store, str(root), timeout=2,
                index_file=index,
                extra_env={"PATH": str(wrapper_dir) + os.pathsep + os.environ["PATH"]},
            )
            future.add_done_callback(lambda _future: cleanup_returned.set())
            try:
                connection, state = fixtures._ready(listener)
                process = processes[0]
                # Git may put /bin/sh between itself and a configured filter.
                # Bind STOP to the executable identity, never the filter's
                # reported immediate parent or an argv substring.
                writer = psutil.Process(state["git_pid"])
                while writer.pid != process.pid and not Path(writer.exe()).samefile(git):
                    parent = writer.parent()
                    assert parent is not None
                    writer = parent
                assert writer.pid != process.pid and Path(writer.exe()).samefile(git)
                descriptor = os.pidfd_open(writer.pid)
                assert os.getpgid(writer.pid) == process.pid != os.getpgrp()
                # Finish orphaning before STOP: POSIX sends HUP+CONT when a
                # stopped group becomes orphaned, which would bypass our barrier.
                wait_until(lambda: fixtures._exited(process.pid), timeout=5,
                           what="fast launcher to exit without reaping it")
                assert lock.exists(), "real Git must own the transaction before STOP"
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
                wait_until(lambda: writer.status() == psutil.STATUS_STOPPED,
                           timeout=5, what="this Git writer to stop")
                controller = workers.submit(
                    _resume_after_term, writer, descriptor, cleanup_returned, mode == "timeout_full_grace",
                )
                armed.set()
                if mode.startswith("interrupt"):
                    with pytest.raises(KeyboardInterrupt) as raised:
                        future.result(timeout=15)
                    assert raised.value is original_interrupt
                else:
                    ok, output, error = future.result(timeout=15)
                    assert not ok and output == "" and "timed out after 2s" in error
                observation = controller.result(timeout=15)
                observation.update({
                    "mode": mode, "git_pid": writer.pid, "pgid": process.pid,
                    "leader_returncode": process.returncode, "lock_exists": lock.exists(),
                    "git_exited": fixtures._exited(writer.pid),
                    "filter_exited": fixtures._exited(state["pid"]),
                    "group_signals": group_signals,
                })
                record_property("controlled_topology", json.dumps(observation, sort_keys=True))
                # First post-cleanup lifecycle assertion: the real Git lock,
                # before any test rescue/release or polling for successful cleanup.
                if mode == "timeout_full_grace":
                    # SIGKILL cannot run Git's unlink handler: Hermes must not
                    # claim this authentic lock or unlink it on Git's behalf.
                    assert lock.exists()
                    term_at = next(at for sig, at in group_signals if sig == signal.SIGTERM)
                    kill_at = next(at for sig, at in group_signals if sig == signal.SIGKILL)
                    assert kill_at - term_at >= 2, "KILL shortened the writer's full TERM grace"
                else:
                    assert not lock.exists(), "fast launcher caused KILL before Git's TERM unlink handler"
                assert observation["git_exited"]
                assert observation["filter_exited"]
                assert "term_pending_at" in observation
                if mode != "timeout_full_grace":
                    assert observation["resumed_at"] - observation["term_pending_at"] < 2
                assert process.returncode is not None
            finally:
                armed.set()
                if descriptor is not None:
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    os.close(descriptor)
                if connection is not None:
                    fixtures._release(connection)
    assert [row["reason"] for row in manager.list_checkpoints(str(root))] == ["initial"]
