"""Read-only database checks with explicit coverage and non-corruption failures.

The deadline is cooperative: SQLite invokes the progress callback between VM
instructions, not while the kernel is blocked on filesystem I/O.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import time

from hermes_state_holders import read_only_db_uri


def database_generation(path: Path) -> list[int]:
    """Metadata identity for binding corruption evidence, without opening a raw fd."""
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def check_database(path: Path, *, full: bool = False, budget_seconds: float = 5.0) -> dict:
    """Report structural readability or full integrity; never restore or mutate data."""
    started = time.monotonic()
    level = "full" if full else "structural"
    result: dict = {"level": level, "status": "incomplete", "full_check": "pending" if full else "deferred"}
    generation = None

    def finish(status: str, message: str) -> dict:
        # Every positive outcome, including every early corruption return,
        # must still refer to the inspected file. An obsolete error cannot
        # authorize restoring whatever now occupies this pathname.
        if status in {"corrupt", "readable", "verified"}:
            try:
                after = database_generation(path)
                changed = (generation is None or
                           (generation != after if full or status == "corrupt" else generation[:2] != after[:2]))
                if changed:
                    status, message = "incomplete", "database file changed or was replaced during the check"
                else:
                    result["generation"] = after
            except (OSError, RuntimeError) as exc:
                status, message = "incomplete", f"cannot confirm database generation: {exc}"
        result.update(status=status, message=message, elapsed_seconds=time.monotonic() - started)
        if full:
            result["full_check"] = "passed" if status == "verified" else "failed" if status == "corrupt" else "incomplete"
        return result

    try:
        info = path.stat()
        result["size_bytes"] = info.st_size
        generation = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if not stat.S_ISREG(info.st_mode):
            return finish("incomplete", "not a regular database file")
        if info.st_size < 100:
            return finish("corrupt", "file is too small for a SQLite database")
        # Read before opening SQLite: closing an unrelated raw fd can drop this
        # process's SQLite locks. The shared helper refuses such a live connection.
        from hermes_cli.sqlite_safe_read import read_header_bytes_preopen
        header = read_header_bytes_preopen(path, length=16)
        if header is None:
            return finish("incomplete", "cannot read database header")
        if header != b"SQLite format 3\0":
            return finish("corrupt", "missing SQLite header magic")
        deadline = started + budget_seconds
        uri = read_only_db_uri(path)
        with closing(sqlite3.connect(uri, uri=True, timeout=min(1.0, budget_seconds))) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA schema_version").fetchone()
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            # Touch only the canonical tables' first leaf/root path, not their
            # rows or indexes end to end. A corrupt sessions root must still
            # reach the existing per-profile recovery guard.
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name IN ('sessions', 'messages')").fetchall()
            for (name,) in tables:
                db.execute(f'SELECT 1 FROM "{name}" NOT INDEXED LIMIT 1').fetchone()
            if full:
                rows = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
                if rows != ["ok"]:
                    return finish("corrupt", "; ".join(rows[:5]))
            if time.monotonic() >= deadline:
                return finish("incomplete", "cooperative time budget exceeded")
        return finish("verified" if full else "readable",
                      "full integrity check passed" if full else "header and schema readable; data/index pages not fully checked")
    except FileNotFoundError:
        return finish("missing", "database file does not exist")
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        corrupt = (code & 0xff) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
        return finish("corrupt" if corrupt else "incomplete", str(exc))
    except (OSError, RuntimeError) as exc:
        return finish("incomplete", str(exc))
