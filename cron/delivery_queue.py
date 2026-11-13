"""Profile-local durable handoff for cron delivery through live gateway adapters.

A restart-safe cron worker executes outside the gateway cgroup.  It cannot own
relay/E2EE adapter objects, so it queues the final send here.  A gateway claims
each row at most once.  If that gateway dies after claiming, the outcome is
marked unknown and never retried: losing a delivery is safer than duplicating a
possibly-completed send.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from agent.deadline import poll_until
from agent.redact import redact_sensitive_text
from cron.executions import _owner_is_live, _process_start_time
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

DELIVERY_DB: Optional[Path] = None
_PROCESS_ID = uuid.uuid4().hex
_lock = threading.RLock()
_ACTIVE_DELIVERIES: set[str] = set()
_TERMINAL = ("delivered", "failed", "unknown", "suppressed")
MAX_TERMINAL_DELIVERIES = 1000
DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS = 300.0


def _prune_terminal_unlocked(conn: sqlite3.Connection) -> None:
    """Redact terminal payloads and retain only bounded outcome metadata."""
    from cron.delivery_thread_receipts import retain_receipts
    retain_receipts(conn)
    conn.execute(
        """UPDATE deliveries SET job_json='{}', content='', media_json=NULL
           WHERE status IN ('delivered','failed','unknown','suppressed')
             AND thread_json IS NULL
             AND (job_json != '{}' OR content != '' OR media_json IS NOT NULL)"""
    )
    keep = max(0, int(MAX_TERMINAL_DELIVERIES))
    terminal_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM deliveries "
            "WHERE status IN ('delivered','failed','unknown','suppressed') AND thread_json IS NULL"
        ).fetchone()[0]
    )
    excess = terminal_count - keep
    if excess > 0:
        conn.execute(
            """INSERT OR IGNORE INTO delivery_tombstones
               (execution_id, terminal_status, finished_at, thread_receipts_json)
               SELECT execution_id, status, finished_at, thread_receipts_json FROM deliveries
               WHERE status IN ('delivered','failed','unknown','suppressed')
                 AND thread_json IS NULL
               ORDER BY julianday(finished_at), finished_at,
                        julianday(created_at), created_at, execution_id
               LIMIT ?""",
            (excess,),
        )
        conn.execute(
            """DELETE FROM deliveries WHERE execution_id IN (
                 SELECT execution_id FROM deliveries
                 WHERE status IN ('delivered','failed','unknown','suppressed')
                   AND thread_json IS NULL
                 ORDER BY julianday(finished_at), finished_at,
                          julianday(created_at), created_at, execution_id
                 LIMIT ?
               )""",
            (excess,),
        )


def queue_path(home: Optional[Path] = None) -> Path:
    """The queue file of ``home`` (the active home when None); a test override wins."""
    if DELIVERY_DB is not None:
        return DELIVERY_DB
    root = Path(home) if home is not None else get_hermes_home()
    return root.resolve() / "cron" / "deliveries.db"


def _path() -> Path:
    return queue_path()


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    # SQLite cannot widen a CHECK in place. Preserve all old rows atomically,
    # including claimed sends, while admitting a distinct never-sent disposition.
    for table in ("deliveries", "delivery_tombstones"):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row and "'suppressed'" not in row[0]:
            conn.execute("SAVEPOINT notification_disposition")
            try:
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                conn.execute(row[0].replace("'unknown'", "'unknown','suppressed'"))
                conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
                conn.execute(f"DROP TABLE {table}_old")
                conn.execute("RELEASE notification_disposition")
            except BaseException:
                conn.execute("ROLLBACK TO notification_disposition")
                conn.execute("RELEASE notification_disposition")
                raise

    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
             execution_id TEXT PRIMARY KEY,
             job_json TEXT NOT NULL,
             content TEXT NOT NULL,
             for_failure INTEGER NOT NULL DEFAULT 0,
             status TEXT NOT NULL CHECK(status IN
               ('pending','delivering','delivered','failed','unknown','suppressed')),
             owner_process_id TEXT,
             owner_pid INTEGER,
             owner_started_at INTEGER,
             created_at TEXT NOT NULL,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_tombstones (
             execution_id TEXT PRIMARY KEY,
             terminal_status TEXT NOT NULL CHECK(terminal_status IN
               ('delivered','failed','unknown','suppressed')),
             finished_at TEXT
           )"""
    )
    add_column_if_missing(
        conn, "deliveries", "for_failure",
        "for_failure INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(conn, 'deliveries', 'media_json', 'media_json TEXT')
    add_column_if_missing(conn, 'deliveries', 'thread_json', 'thread_json TEXT')
    for table in ('deliveries', 'delivery_tombstones'):
        add_column_if_missing(conn, table, 'thread_receipts_json', 'thread_receipts_json TEXT')


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from hermes_cli.sqlite_util import open_db

    path = _path()
    conn = open_db(path, db_label="cron/deliveries.db", synchronous_full=True, initialize=_initialize_schema)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    # Pruning is done explicitly by the paths that create terminal
    # rows (_finish / recover_abandoned / _terminalize_wait_timeout);
    # read-only polls must not pay for a full-table UPDATE + COUNT.
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def enqueue(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
) -> dict:
    """Persist one idempotent delivery request before the worker waits."""
    with _transaction() as conn:
        # Serialize the tombstone check and insert with retention in other
        # processes, which can move a terminal delivery into the tombstone table.
        conn.execute("BEGIN IMMEDIATE")
        tombstone = conn.execute(
            "SELECT terminal_status, finished_at FROM delivery_tombstones "
            "WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        if tombstone is not None:
            return {
                "execution_id": str(execution_id),
                "status": tombstone["terminal_status"],
                "finished_at": tombstone["finished_at"],
            }
        existing = conn.execute('SELECT * FROM deliveries WHERE execution_id=?', (str(execution_id),)).fetchone()
        if existing is not None:
            return dict(existing)
        from cron.strict_delivery import media_identity
        media = json.dumps(media_identity(content), ensure_ascii=False)
        conn.execute(
            """INSERT OR IGNORE INTO deliveries
               (execution_id, job_json, content, for_failure, status, created_at, media_json)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (
                str(execution_id),
                json.dumps(job, ensure_ascii=False, sort_keys=True),
                str(content),
                int(bool(for_failure)),
                _hermes_now().isoformat(),
                media,
            ),
        )
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return dict(row)


def get_status(execution_id: str) -> Optional[dict]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
        if row is not None:
            return dict(row)
        tombstone = conn.execute(
            "SELECT execution_id, terminal_status, finished_at, thread_receipts_json "
            "FROM delivery_tombstones WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
    if tombstone is None:
        return None
    return {
        "execution_id": tombstone["execution_id"],
        "status": tombstone["terminal_status"],
        "finished_at": tombstone["finished_at"],
        "error": None,
        "thread_receipts_json": tombstone["thread_receipts_json"],
    }


def claim_next(*, execution_id=None, expected=None, thread_plan=None) -> Optional[dict]:
    """Atomically claim one pending send before touching the transport."""
    pid = os.getpid()
    started = _process_start_time(pid)
    with _transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # created_at carries a DST-varying offset: order by instant, not text.
        row = conn.execute(
            "SELECT * FROM deliveries WHERE status='pending' AND (? IS NULL OR execution_id=?) "
            "ORDER BY julianday(created_at), created_at, execution_id LIMIT 1", (execution_id, execution_id)
        ).fetchone()
        if row is None:
            return None
        if expected is not None and any(row[key] != expected[key] for key in ('execution_id', 'job_json', 'content', 'for_failure', 'media_json', 'thread_json')):
            return None
        if row['thread_json'] is not None and thread_plan is None:
            return None
        if thread_plan is not None:
            from cron.strict_thread_delivery import bind_row, encode
            thread_plan = encode(bind_row(row, thread_plan))
        cur = conn.execute(
            """UPDATE deliveries SET status='delivering', owner_process_id=?,
               owner_pid=?, owner_started_at=?, thread_json=?
               WHERE execution_id=? AND status='pending'""",
            (_PROCESS_ID, pid, started, thread_plan, row["execution_id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (row["execution_id"],)
        ).fetchone()
        _ACTIVE_DELIVERIES.add(row["execution_id"])
    result = dict(claimed)
    result["job"] = json.loads(result.pop("job_json"))
    return result


def claim_thread_create(execution_id, index):
    from cron.strict_thread_delivery import transition
    return transition(execution_id, index, 'initial', 'create_claimed')


def claim_thread_body(execution_id, index):
    from cron.strict_thread_delivery import transition
    return transition(execution_id, index, 'root_acked', 'body_claimed')


def claim_fanout_target(execution_id, index):
    from cron.strict_thread_delivery import transition
    return transition(execution_id, index, 'initial', 'target_claimed')


def _finish(execution_id: str, *, error: Optional[str], suppressed: bool = False) -> bool:
    from cron.strict_delivery import DeferredDelivery, UnknownDelivery
    safe_error = (
        redact_sensitive_text(str(error), force=True, redact_url_credentials=True)
        if error
        else None
    )
    if isinstance(error, DeferredDelivery):
        with _transaction() as conn:
            cur = conn.execute(
                """UPDATE deliveries SET status='pending', owner_process_id=NULL,
                       owner_pid=NULL, owner_started_at=NULL, finished_at=NULL, error=NULL
                   WHERE execution_id=? AND status='delivering'
                     AND owner_process_id=? AND owner_pid=?""",
                (execution_id, _PROCESS_ID, os.getpid()),
            )
        return cur.rowcount == 1
    status = "unknown" if isinstance(error, UnknownDelivery) else "failed" if error else "suppressed" if suppressed else "delivered"
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE deliveries SET status=?, finished_at=?, error=?
               WHERE execution_id=? AND status='delivering'
                 AND owner_process_id=? AND owner_pid=?""",
            (
                status,
                _hermes_now().isoformat(),
                safe_error,
                execution_id,
                _PROCESS_ID,
                os.getpid(),
            ),
        )
        _prune_terminal_unlocked(conn)
    return cur.rowcount == 1


def _abandoned_job_id(row):
    job = json.loads(row['job_json'])
    if not isinstance(job, dict) or (job.get('id') is not None and not isinstance(job['id'], str)):
        raise ValueError('invalid abandoned delivery job')
    return job.get('id')


def _delivery_owner_is_live(row):
    pid, started = row['owner_pid'], row['owner_started_at']
    if (type(pid) is not int or pid <= 0 or not isinstance(row['owner_process_id'], str)
            or not row['owner_process_id']
            or (started is not None and (type(started) is not int or started <= 0))):
        raise ValueError('invalid abandoned delivery owner')
    if started is None:
        from gateway.status import _pid_exists
        return _pid_exists(pid)  # Missing identity cannot disprove an existing owner.
    return _owner_is_live(pid, started)


def recover_abandoned() -> int:
    """Fence dead delivery owners as unknown; never replay uncertain sends.

    The firer settled long ago against a ``queued`` row, so each fenced row's outcome is written
    back here (job ``delivery_failed`` + error, ledger ``failed``) instead of leaving the run at
    ``delivery_queued`` forever."""
    fenced: list[tuple[Optional[str], str]] = []
    with _transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            "SELECT * "
            "FROM deliveries WHERE status='delivering'"
        ).fetchall()
        for row in rows:
            same_process = row["owner_process_id"] == _PROCESS_ID
            try:
                owner_live = _delivery_owner_is_live(row)
                if same_process:
                    if row['owner_pid'] != os.getpid():
                        raise ValueError('abandoned delivery process identity conflict')
                    with _lock:
                        if row['execution_id'] in _ACTIVE_DELIVERIES:
                            continue
                elif owner_live:
                    continue
                job_id = _abandoned_job_id(row)
                if row['thread_json'] is not None:
                    from cron.strict_thread_delivery import recoverable
                    if recoverable(row):
                        conn.execute("UPDATE deliveries SET status='pending', owner_process_id=NULL, owner_pid=NULL, "
                                     "owner_started_at=NULL WHERE execution_id=? AND status='delivering'",
                                     (row['execution_id'],))
                        continue
            except (TypeError, ValueError, KeyError, OSError):
                logger.warning('Cron delivery %s retained unchanged: recovery evidence invalid',
                               row['execution_id'], exc_info=True)
                continue
            error = (
                "Gateway finished delivery but could not persist its outcome; "
                "send was not retried."
                if same_process
                else "Gateway exited during delivery; send outcome is unknown and was not retried."
            )
            cur = conn.execute(
                """UPDATE deliveries SET status='unknown', finished_at=?, error=?
                   WHERE execution_id=? AND status='delivering'""",
                (
                    _hermes_now().isoformat(),
                    error,
                    row["execution_id"],
                ),
            )
            if cur.rowcount:
                # Read the job id now: pruning below redacts a terminal row's job_json.
                fenced.append((job_id, row['execution_id']))
        _prune_terminal_unlocked(conn)
    from cron.delivery_outcome import settle_quietly

    for job_id, execution_id in fenced:
        settle_quietly(job_id, execution_id)
    return len(fenced)


def _prepared_next(prepare, excluded):
    from contextlib import closing
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM deliveries WHERE status='pending' "
            "ORDER BY julianday(created_at), created_at, execution_id").fetchall()
    for row in rows:
        if row['execution_id'] in excluded:
            continue
        excluded.add(row['execution_id'])
        try:
            from cron.strict_delivery import media_identity
            media = media_identity(row['content'])
            if media and (row['media_json'] is None or media != json.loads(row['media_json']) or any(item[2] is None for item in media)):
                raise RuntimeError('cron media identity unavailable or changed')
            plan = prepare(json.loads(row['job_json']), row['content'], bool(row['for_failure']))
            if row['thread_json'] is not None or (isinstance(plan, dict) and plan.get('thread') is not None):
                from cron.strict_thread_delivery import bind_row
                bind_row(row, plan.get('thread'))
            if media_identity(row['content']) != media:
                raise RuntimeError('cron media changed during preflight')
            from cron.strict_bot_delivery import waiting
            if waiting(row, plan):
                continue
        except Exception:
            logger.warning('Cron delivery %s retained pending: transport preflight refused', row['execution_id'], exc_info=True)
            continue
        claim_args = {'thread_plan': plan['thread']} if isinstance(plan, dict) and plan.get('thread') is not None else {}
        claimed = claim_next(execution_id=row['execution_id'], expected=row, **claim_args)
        if claimed is not None:
            return claimed, plan
    return None, None


def drain(
    send: Callable[[dict, str, bool], Optional[str]], *, limit: int = 20, prepare=None
) -> int:
    """Deliver pending rows through *send*, terminalizing every claimed row."""
    recover_abandoned()
    processed = 0
    excluded = set()
    for _ in range(max(0, limit)):
        row, plan = _prepared_next(prepare, excluded) if prepare is not None else (claim_next(), None)
        if row is None:
            break
        with _lock:
            _ACTIVE_DELIVERIES.add(row["execution_id"])
        try:
            try:
                if prepare is None:
                    error = send(row["job"], row["content"], bool(row["for_failure"]))
                else:
                    from cron.strict_delivery import delivery_scope
                    with delivery_scope(row['execution_id'], plan):
                        error = send(row["job"], row["content"], bool(row["for_failure"]))
            except BaseException as exc:
                from cron.strict_delivery import DeferredDelivery, UnknownDelivery
                if isinstance(exc, DeferredDelivery):
                    error = exc
                else:
                    error = f"{type(exc).__name__}: {exc}"
                    if prepare is not None:
                        error = UnknownDelivery(error)
            if _finish(row["execution_id"], error=error,
                       suppressed=bool(row["job"].get("_notification_all_targets_suppressed"))):
                from cron.delivery_outcome import settle_quietly

                settle_quietly(row["job"].get("id"), row["execution_id"])
        finally:
            with _lock:
                _ACTIVE_DELIVERIES.discard(row["execution_id"])
        processed += 1
    return processed


def _terminalize_wait_timeout(execution_id: str) -> str:
    """Fence a delivery whose worker can no longer wait for confirmation.

    A row still ``pending`` was provably never attempted, so it is left queued
    for whichever gateway comes up next (a restart that includes an update can
    easily exceed the worker's wait budget).  That is a deferral, not a
    failure: report success so the job is not recorded ``delivery_failed`` for
    a message the drain will still send.  Only a row caught mid-send is
    uncertain and gets fenced ``unknown``.
    """
    now = _hermes_now().isoformat()
    uncertain_error = (
        "timed out while gateway delivery was in progress; outcome is unknown and "
        "was not retried"
    )
    with _transaction() as conn:
        row = conn.execute(
            "SELECT status FROM deliveries WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        if row is not None and row["status"] == "pending":
            logger.warning(
                "Cron delivery %s: no live gateway within the wait budget; "
                "left queued for the next gateway",
                execution_id,
            )
            return ""
        conn.execute(
            """UPDATE deliveries SET status='unknown', finished_at=?, error=?
               WHERE execution_id=? AND status='delivering'""",
            (now, uncertain_error, str(execution_id)),
        )
        row = conn.execute(
            "SELECT status, error FROM deliveries WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        _prune_terminal_unlocked(conn)
    if row is None:
        return "timed out waiting for live gateway delivery"
    if row["status"] in {"delivered", "suppressed"}:
        return ""
    return str(row["error"] or f"delivery {row['status']}")


def enqueue_and_wait(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Queue delivery and wait for a gateway's terminal at-most-once outcome."""
    queued = enqueue(execution_id, job, content, for_failure=for_failure)
    if queued["status"] in _TERMINAL:
        return None if queued["status"] in {"delivered", "suppressed"} else str(
            queued.get("error") or f"delivery {queued['status']}"
        )
    wait_timeout = (
        DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    )
    def _terminal_row() -> Optional[dict]:
        row = get_status(execution_id)
        return row if row and row["status"] in _TERMINAL else None

    row = poll_until(_terminal_row, wait_timeout, 1.0)
    if row is None:
        return _terminalize_wait_timeout(execution_id) or None
    return None if row["status"] in {"delivered", "suppressed"} else str(
        row.get("error") or f"delivery {row['status']}"
    )
