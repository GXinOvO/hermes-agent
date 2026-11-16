"""Common advisory lock for POSIX plugin tree writers.

The lock lives outside plugins/ so replacing/removing a whole plugin tree cannot
replace its lock inode. Callers must hold it before reading expected bytes and
through publication, rollback and directory fsync. Uncooperative external writers
are outside this contract; this is not a filesystem-wide compare-and-swap.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

_held = threading.local()


@contextmanager
def p1_target_file_writer(target):
    """Archive/snapshot writers participate when the destination is inside the P1 plugin."""
    plugin = _canonical_plugin_for_target(Path(target))
    if plugin is not None and os.name == "posix":
        with target_writer_lock(plugin):
            yield
    else:
        yield


def target_writer_lock_path(plugin_dir):
    plugin_dir = _canonical_plugin_dir(plugin_dir)
    return plugin_dir.parent.parent / ".plugin-writer-locks" / (plugin_dir.name + ".lock")


def _canonical_plugin_dir(plugin_dir: Path) -> Path:
    plugin_dir = Path(plugin_dir)
    try:
        resolved = plugin_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("plugin target identity cannot be resolved") from exc
    if resolved.parent.name != "plugins":
        raise ValueError("plugin target is outside the plugins directory")
    return resolved


def _canonical_plugin_for_target(target: Path) -> Path | None:
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("plugin target identity cannot be resolved") from exc
    for candidate in (resolved, *resolved.parents):
        if candidate.name == "cron-supervisor-report-budget" and candidate.parent.name == "plugins":
            return candidate
    return None


@contextmanager
def target_writer_lock(plugin_dir):
    if os.name != "posix":
        raise NotImplementedError("plugin target writer contract requires POSIX flock")
    import fcntl

    path = target_writer_lock_path(plugin_dir)
    key = str(path)
    held = _held.__dict__.setdefault("paths", {})
    if key in held:
        if held[key]["pid"] != os.getpid():
            raise RuntimeError("inherited target writer lock cannot be reused after fork")
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("target writer lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner_pid = os.getpid()
        held[key] = {"pid": owner_pid, "fd": fd}
        try:
            yield
        finally:
            held.pop(key, None)
            if os.getpid() == owner_pid:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
