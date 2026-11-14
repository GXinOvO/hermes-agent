from contextlib import contextmanager
from pathlib import Path
import os
import threading

from hermes_constants import mkdir_under_hermes_home
from pm.filesystem import lock_fd, unlock_fd

_held = threading.local()


class InstallInProgress(RuntimeError):
    pass


@contextmanager
def install_gate(home, *, exclusive=False, allow_marker=False, timeout=0.0):
    if os.name != "posix":
        if exclusive:
            raise NotImplementedError("Cron plugin installation requires POSIX locking")
        yield
        return
    cron = Path(home).resolve() / "cron"
    key = str(cron)
    held = _held.__dict__.setdefault("homes", {})
    if key in held:
        if exclusive and not held[key]:
            raise InstallInProgress("Cannot upgrade an active Cron admission")
        yield
        return
    mkdir_under_hermes_home(cron)
    with (cron / ".install.lock").open("a+b") as handle:
        if os.name == "posix" and not exclusive:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallInProgress("Cron installation gate is held") from exc
        elif not lock_fd(handle.fileno(), wait=timeout > 0, timeout=timeout):
            raise InstallInProgress("Cron installation gate is held")
        try:
            marker = cron / ".install-pending.json"
            if not allow_marker and (marker.exists() or marker.is_symlink()):
                raise InstallInProgress("Cron installation requires reconciliation")
            held[key] = exclusive
            try:
                yield
            finally:
                held.pop(key)
        finally:
            unlock_fd(handle.fileno())
