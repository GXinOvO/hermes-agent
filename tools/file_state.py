"""Cross-agent file state coordination.

Prevents mangled edits when concurrent subagents (same process, same
filesystem) touch the same file: B writes a file A already read, so A's next
write would clobber B's changes. Complements the single-agent path-overlap
check in ``agent.tool_dispatch_helpers._should_parallelize_tool_batch``. A process-wide
``FileStateRegistry`` tracks per-agent read stamps, the global last writer and
a per-path lock; every method is a no-op under ``HERMES_DISABLE_FILE_STATE_GUARD=1``.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from tools.file_backend_io import FileBackendUnavailable, file_mtime, storage_path

# (mtime, read_ts, partial). partial=True when read_file returned a windowed
# view (offset > 1 or limit < total_lines) — a later write should still warn
# so the model re-reads in full.
ReadStamp = tuple[float, float, bool]

# Bounded so long sessions don't accumulate unbounded state.
_MAX_PATHS_PER_AGENT = 4096
_MAX_GLOBAL_WRITERS = 4096


def _disabled() -> bool:
    # Re-read each call so tests can toggle via monkeypatch.setenv.
    return os.environ.get("HERMES_DISABLE_FILE_STATE_GUARD", "").strip() == "1"


def guard_disabled() -> bool:
    """True when the user switched the read-before-write guard off; the file
    tools then warn instead of refusing stale/unread write_file overwrites."""
    return _disabled()


def _mtime_or_none(resolved: str) -> Optional[float]:
    from tools.environments.sandbox_scope import hosted_scope
    if hosted_scope() is not None:
        return None  # a hosted agent's path names its sandbox, not this host
    try:
        return file_mtime(resolved)
    except OSError:
        return None


def _fmt_ts(ts: float) -> str:
    # Short wall-clock for warnings; avoids datetime formatting on the hot path.
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _evict_oldest(container, cap: int) -> None:
    """Pop entries until *container* is within *cap* (sets: arbitrary; dicts: oldest
    by insertion order). An eviction only costs one redundant re-send or staleness check."""
    for _ in range(len(container) - cap):
        try:
            if isinstance(container, set):
                container.pop()
            else:
                container.pop(next(iter(container)))
        except (StopIteration, KeyError):
            break


class _WriteObserver:
    def __init__(self, registry, exclude_task_id: str, paths: Iterable[str]):
        self.registry = registry
        self.exclude_task_id = exclude_task_id
        self.paths = frozenset(paths)
        names = registry._read_names.get(exclude_task_id, {})
        self.names = {path: names.get(path, path) for path in self.paths}
        self.modified: set[str] = set()

    def snapshot(self) -> list[str]:
        if _disabled():
            return []
        with self.registry._state_lock:
            paths = sorted(self.modified)
        return [self.names[path] for path in paths]

    def close(self) -> None:
        with self.registry._state_lock:
            self.registry._write_observers.discard(self)


class FileStateRegistry:
    """Process-wide coordinator for cross-agent file edits."""

    def __init__(self) -> None:
        self._reads: dict[str, dict[str, ReadStamp]] = defaultdict(dict)
        self._read_names: dict[str, dict[str, str]] = defaultdict(dict)
        self._writer_names: dict[str, str] = {}
        self._last_writer: dict[str, tuple[str, float]] = {}
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_lock_users: dict[str, int] = {}
        self._meta_lock = threading.Lock()  # guards _path_locks
        self._state_lock = threading.Lock()  # guards _reads + _last_writer
        self._write_observers: set[_WriteObserver] = set()

    @contextmanager
    def lock_path(self, resolved: str):
        """Per-path lock: threads on the same path serialize, different paths proceed.
        The lock entry is dropped once the last holder/waiter exits."""
        resolved = storage_path(resolved)
        with self._meta_lock:
            lock = self._path_locks.setdefault(resolved, threading.Lock())
            self._path_lock_users[resolved] = self._path_lock_users.get(resolved, 0) + 1
        acquired = False
        try:
            acquired = lock.acquire(timeout=15)
            if not acquired:
                raise FileBackendUnavailable('file-state path lock deadline exceeded; refusing write')
            yield
        finally:
            if acquired:
                lock.release()
            with self._meta_lock:
                users = self._path_lock_users[resolved] - 1
                if users:
                    self._path_lock_users[resolved] = users
                else:
                    self._path_lock_users.pop(resolved, None)
                    self._path_locks.pop(resolved, None)

    def _stamp(self, task_id: str, resolved: str, mtime: float, now: float, partial: bool, display: str) -> None:
        """Caller holds ``_state_lock``."""
        agent_reads = self._reads[task_id]
        previous = agent_reads.get(resolved)
        if previous is not None and previous[0] == float(mtime):
            partial = previous[2] and bool(partial)
        agent_reads[resolved] = (float(mtime), now, bool(partial))
        self._read_names[task_id][resolved] = display
        _evict_oldest(agent_reads, _MAX_PATHS_PER_AGENT)
        self._read_names[task_id] = {p: name for p, name in self._read_names[task_id].items() if p in agent_reads}

    def record_read(self, task_id: str, resolved: str, *, partial: bool = False,
                    mtime: Optional[float] = None) -> None:
        if _disabled():
            return
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        display, resolved = resolved, storage_path(resolved)
        with self._state_lock:
            self._stamp(task_id, resolved, mtime, time.time(), partial, display)

    def note_write(self, task_id: str, resolved: str, *, mtime: Optional[float] = None) -> None:
        """Record a successful write: global last-writer AND this agent's own
        read stamp (a write is an implicit read of the current content)."""
        if _disabled():
            return
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        display, resolved = resolved, storage_path(resolved)
        with self._state_lock:
            now = time.time()
            self._last_writer[resolved] = (task_id, now)
            self._writer_names[resolved] = display
            _evict_oldest(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._writer_names = {p: name for p, name in self._writer_names.items() if p in self._last_writer}
            self._stamp(task_id, resolved, mtime, now, False, display)
            for observer in self._write_observers:
                if task_id != observer.exclude_task_id and resolved in observer.paths:
                    observer.modified.add(resolved)

    def check_stale(self, task_id: str, resolved: str) -> Optional[str]:
        """Model-facing warning if this write would be stale, else ``None``. Severity
        order: sibling wrote after our read > mtime drift / partial read > never read."""
        if _disabled():
            return None
        key = storage_path(resolved)
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(key)
            last_writer = self._last_writer.get(key)

        if stamp is None and last_writer is None:  # net-new file / first touch
            return None
        current_mtime = _mtime_or_none(resolved)
        if current_mtime is None:
            return None  # file doesn't exist — write creates it; not stale

        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} but this agent never read it. "
                        "Read the file before writing to avoid overwriting "
                        "the sibling's changes.")
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this agent's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing.")

        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing.")
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Read the remaining pages, or use patch, "
                    "before overwriting it.")
            return None

        return (
            f"{resolved} was not read by this agent. "
            "Read the file first so you can write an informed edit.")

    def observe_writes(self, exclude_task_id: str) -> tuple[float, list[str], Optional[_WriteObserver]]:
        with self._state_lock:
            since_ts = time.time()
            paths = list(self._reads.get(exclude_task_id, {})) if not _disabled() else []
            observer = _WriteObserver(self, exclude_task_id, paths) if paths else None
            if observer is not None:
                self._write_observers.add(observer)
            return since_ts, paths, observer

    def writes_since(self, exclude_task_id: str, since_ts: float,
                     paths: Iterable[str]) -> dict[str, list[str]]:
        """``{writer_task_id: [paths]}`` for writes after ``since_ts`` by agents
        other than ``exclude_task_id`` (delegate_task's "subagent modified files
        you previously read" reminder)."""
        if _disabled():
            return {}
        paths_set = {storage_path(p) for p in paths}
        out: dict[str, list[str]] = defaultdict(list)
        with self._state_lock:
            for p, (writer_tid, ts) in self._last_writer.items():
                if writer_tid != exclude_task_id and ts >= since_ts and p in paths_set:
                    out[writer_tid].append(self._read_names.get(exclude_task_id, {}).get(p, self._writer_names.get(p, p)))
        return dict(out)

    def known_reads(self, task_id: str) -> list[str]:
        """Resolved paths this agent has read."""
        if _disabled():
            return []
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    def display_paths(self, task_id: str, paths: Iterable[str]) -> list[str]:
        with self._state_lock:
            names = self._read_names.get(task_id, {})
            return [names.get(p, self._writer_names.get(p, p)) for p in paths]

    def forget_task(self, task_id: str) -> None:
        """Release read stamps and writer claims owned by a task after its lifecycle ends.

        A finished task is not a concurrent sibling: leaving its writer claims behind makes
        the next run of the same job (a fresh ``cron:<job>:<uuid>`` id) refuse to write the
        same scratch path as "modified by sibling subagent" hours after the writer exited."""
        with self._state_lock:
            self._reads.pop(task_id, None)
            self._read_names.pop(task_id, None)
            for p in [p for p, (writer_tid, _ts) in self._last_writer.items() if writer_tid == task_id]:
                del self._last_writer[p]
                self._writer_names.pop(p, None)

    def clear(self) -> None:
        """Reset all state. Intended for tests only."""
        with self._state_lock:
            self._reads.clear()
            self._read_names.clear()
            self._writer_names.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()
            self._path_lock_users.clear()


_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


# Convenience wrappers (short names used at call sites).
def record_read(task_id: str, resolved_or_path: str | Path, *, partial: bool = False) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial)


def note_write(task_id: str, resolved_or_path: str | Path) -> None:
    _registry.note_write(task_id, str(resolved_or_path))


def check_stale(task_id: str, resolved_or_path: str | Path) -> Optional[str]:
    return _registry.check_stale(task_id, str(resolved_or_path))


def lock_path(resolved_or_path: str | Path):
    return _registry.lock_path(str(resolved_or_path))


def writes_since(exclude_task_id: str, since_ts: float, paths: Iterable[str | Path]) -> dict[str, list[str]]:
    return _registry.writes_since(exclude_task_id, since_ts, [str(p) for p in paths])


def known_reads(task_id: str) -> list[str]:
    return _registry.known_reads(task_id)


__all__ = [
    "FileStateRegistry",
    "check_stale",
    "get_registry",
    "known_reads",
    "lock_path",
    "note_write",
    "record_read",
    "writes_since",
]
