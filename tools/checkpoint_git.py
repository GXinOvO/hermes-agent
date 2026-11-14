"""Checkpoint Git process lifetime, including Git's signal-driven lock cleanup."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

from hermes_cli._subprocess_compat import kill_process_tree, windows_hide_flags


logger = logging.getLogger(__name__)
_TERMINATE_GRACE_SECONDS = 2


def _terminate_git_process(process: subprocess.Popen, pgid: int | None) -> None:
    """The writer's grace belongs to its group, not the launcher we can reap."""
    if os.name == "nt":
        kill_process_tree(process)
        return

    from agent.deadline import kill_process_tree as signal_tree
    from tools.environments.local import _wait_for_group_exit

    try:
        if process.returncode is None:
            # Preserve the identity-aware descendant sweep, including helpers
            # outside the process group, while ancestry is still observable.
            signal_tree(process.pid, sig=signal.SIGTERM)
        if pgid is not None:
            # communicate() may already have reaped the launcher. Its PGID
            # remains reserved while any of its members (the writer) survives.
            os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only (nt returned above)
        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        remaining = max(0.0, deadline - time.monotonic())
        if pgid is not None:
            if _wait_for_group_exit(process, pgid, remaining):
                return
        else:
            process.wait(timeout=remaining)
            return
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        pass

    # The generic grace helper intentionally escalates as soon as the leader
    # exits. Only call its force path after the group's monotonic grace expired.
    if process.returncode is None:
        kill_process_tree(process)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only (nt returned above)
        except ProcessLookupError:
            pass


def run_git_process(cmd: list[str], *, env: dict, cwd: str, timeout: int,
                    nul_output: bool = False) -> subprocess.CompletedProcess:
    """Let Git release its locks before forcing a timed-out process tree to exit.

    A direct SIGKILL (subprocess.run's timeout path) skips Git's lockfile
    handlers. The private process group also contains filters/helpers that
    would otherwise outlive Git and keep captured pipes open. Existing lock
    files are never removed by Hermes.
    """
    text_options = {} if nul_output else {"text": True, "encoding": "utf-8", "errors": "replace"}
    # detached-spawn: ok — awaited Git; interrupted calls terminate and reap its tree
    process_options: dict = {"process_group": 0} if os.name != "nt" else {}
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, env=env, cwd=cwd,
        creationflags=windows_hide_flags(), **text_options, **process_options,
    ) as process:
        # Capture ownership before communicate()/wait() can reap the leader.
        # A shared-group fallback must never authorize a group-wide signal.
        pgid = None
        if os.name != "nt":
            try:
                if os.getpgid(process.pid) == process.pid:
                    pgid = process.pid
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                _terminate_git_process(process, pgid)
            except Exception:
                logger.warning("Checkpoint Git termination failed", exc_info=True)
            try:
                process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except (OSError, ValueError, subprocess.SubprocessError):
                # Cleanup must not turn an interrupt into an ordinary Git failure.
                logger.warning("Checkpoint Git output drain failed after termination", exc_info=True)
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
