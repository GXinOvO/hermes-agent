"""Read-only diagnostics for project index locks; observation is not ownership."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat


_PROJECT_KEY = re.compile(r"[0-9a-f]{16}")


def refuse_locked_index(store: Path, working_dir: str) -> None:
    """Stop a restore before it can plan changes using an obstructed index."""
    from tools.checkpoint_manager import _index_path, _project_hash
    from tools.checkpoint_pruning import PruneError

    index = _index_path(store, _project_hash(working_dir))
    if os.path.lexists(str(index) + ".lock"):
        raise PruneError(f"Restore blocked by project index lock: {index}.lock")


def delete_restore_paths(index: Path, working_dir: str, paths: list[str]) -> list[str]:
    """Arbitrate Git writers for Python-only deletes; release only our own lock."""
    from tools.checkpoint_pruning import PruneError

    if not paths:
        return []
    lock = index.with_name(index.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PruneError(f"Restore blocked by project index lock: {lock}") from exc
    owned = os.fstat(fd)
    failed = []
    try:
        for rel in paths:
            try:
                target = Path(working_dir) / rel
                if target.is_file() or target.is_symlink():
                    target.unlink()
            except OSError as exc:
                logging.getLogger(__name__).warning("Safe restore: could not remove %s: %s", rel, exc)
                failed.append(rel)
    finally:
        os.close(fd)
        try:
            current = lock.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass
    return failed


def _project_workdir(meta_path: Path, key: str) -> str:
    before = meta_path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(meta_path, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > 65536):
            raise ValueError("project metadata is not a stable, bounded regular file")
        with os.fdopen(fd, encoding="utf-8-sig", closefd=False) as handle:
            meta = json.loads(handle.read(65537))
        workdir = meta.get("workdir") if isinstance(meta, dict) else None
        if (not isinstance(workdir, str) or not Path(workdir).is_absolute()
                or hashlib.sha256(workdir.encode()).hexdigest()[:16] != key):
            raise ValueError("project metadata does not match index identity")
        return workdir
    finally:
        os.close(fd)


def index_lock_status() -> dict:
    """Inspect index metadata without Git, project traversal or lock removal.

    Git locks do not contain a reliable owner identity. In particular, neither
    old timestamps nor a local process scan authorizes recovery on a shared
    filesystem. A row is a point-in-time observation, not a maintenance lease.
    """
    from tools.checkpoint_manager import _resolve_checkpoint_base, _store_path

    base = _resolve_checkpoint_base()
    store = _store_path(base)
    result = {"base": str(base), "store": str(store), "locks": [], "errors": []}
    try:
        paths = sorted((store / "indexes").iterdir())
    except FileNotFoundError:
        return result
    except OSError as exc:
        result["errors"].append(str(exc))
        return result
    for path in paths:
        if not path.name.endswith(".lock"):
            continue
        key = path.name[:-5]
        if not _PROJECT_KEY.fullmatch(key):
            result["errors"].append(f"Unrecognized project index lock name: {path.name}")
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # Git completed between listing and observation.
        except OSError as exc:
            result["errors"].append(str(exc))
            continue
        row = {
            "project_hash": key, "lock_path": str(path), "workdir": None,
            "identity": {"device": info.st_dev, "inode": info.st_ino,
                         "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                         "ctime_ns": info.st_ctime_ns},
            "kind": "regular" if stat.S_ISREG(info.st_mode) else "non-regular",
            "ownership": "unknown", "recovery_allowed": False,
        }
        meta_path = store / "projects" / (key + ".json")
        try:
            row["workdir"] = _project_workdir(meta_path, key)
        except (OSError, ValueError, RecursionError) as exc:
            row["metadata_error"] = str(exc)
        result["locks"].append(row)
    return result
