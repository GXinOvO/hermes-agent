"""Only old sealed Bot results still need their native history row as proof."""
import json
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)


def _legacy_job(job, execution_id, home):
    from cron.sealed_bot_result import _definition, _hash
    if not isinstance(job, dict):
        return None
    value = job.get('_sealed_bot_result_v1')
    if (not isinstance(value, dict) or value.get('execution') is not None
            or value.get('source_home') != str(home) or value.get('job_id') != job.get('id')
            or value.get('execution_id') != execution_id or job.get('execution_id') != execution_id
            or value.get('job_sha256') != _definition(job)
            or value.get('result_sha256') != _hash(value.get('result'))
            or value.get('seal') != _hash({k: v for k, v in value.items() if k != 'seal'})):
        return None
    return job.get('id')


def legacy_dependencies(home):
    from cron.delivery_queue import queue_path
    from cron.sealed_bot_result import delivery_identity
    path = queue_path(home)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('sealed Cron queue symlink during history retention')
    if not path.exists():
        return {}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        pending = db.execute("SELECT execution_id,job_json FROM deliveries WHERE status IN ('pending','delivering')").fetchall()
        outcomes = dict(db.execute('SELECT execution_id,status FROM deliveries'))
        outcomes.update(db.execute('SELECT execution_id,terminal_status FROM delivery_tombstones'))
    keep = {}
    for execution_id, payload in pending:
        try:
            job_id = _legacy_job(json.loads(payload), execution_id, home)
            if job_id:
                keep[execution_id] = job_id
        except (TypeError, ValueError):
            logger.warning('Cron delivery %s has invalid legacy seal evidence', execution_id)
    root = home / 'cron' / 'bot_chat_pending'
    if root.is_symlink():
        raise ValueError('sealed Cron deferred store symlink during history retention')
    for entry in root.glob('*.json'):
        try:
            if entry.is_symlink():
                raise ValueError('sealed Cron deferred record symlink')
            record = json.loads(entry.read_text(encoding='utf-8-sig'))
            job = record['job']
            execution_id = job['execution_id']
            job_id = _legacy_job(job, execution_id, home)
            if (job_id and record.get('status') in {'queued', 'claimed'}
                    and outcomes.get(execution_id) in {'pending','delivering','delivered','unknown'}
                    and record.get('id') == entry.stem == delivery_identity(home, job_id, execution_id, record['home'])):
                keep[execution_id] = job_id
        except (KeyError, TypeError, ValueError, OSError):
            logger.warning('Cron deferred record %s has invalid legacy seal evidence', entry.name)
    return keep


def prune_history(conn, cap):
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    from hermes_constants import get_hermes_home
    home = path.parent.parent if path.parent.name == 'cron' else get_hermes_home().resolve()
    try:
        keep = legacy_dependencies(home)
    except (ValueError, OSError, sqlite3.Error):
        logger.warning('Cron history retention deferred: dependency evidence unreadable', exc_info=True)
        return
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS retained_bot_executions (id TEXT PRIMARY KEY, job_id TEXT)')
    conn.execute('DELETE FROM retained_bot_executions')
    conn.executemany('INSERT INTO retained_bot_executions VALUES (?, ?)', keep.items())
    conn.execute("""DELETE FROM executions WHERE id IN (
        SELECT id FROM executions
        WHERE status IN ('completed','failed','unknown','cancelled','missed')
          AND NOT EXISTS (SELECT 1 FROM retained_bot_executions p WHERE p.id=executions.id AND p.job_id=executions.job_id)
        ORDER BY julianday(finished_at) DESC, finished_at DESC,
                 julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
        )""", (max(0, int(cap)),))
