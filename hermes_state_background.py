"""Detached background results in the profile's existing SessionDB metadata."""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time

PREFIX = 'background-result:'
RETENTION_SECONDS = 7 * 24 * 3600
MAX_RESULTS = 64
MAX_IDENTITIES = 65536
logger = logging.getLogger(__name__)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _key(task_id):
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError('invalid background task identity')
    return PREFIX + hashlib.sha256(task_id.encode()).hexdigest()


def _owner_live(record):
    from cron.executions import _owner_is_live
    return _owner_is_live(record['owner_pid'], record['owner_started_at'])


def _load(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return _decode(key, row[0]) if row else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _decode(key, raw):
    record = json.loads(raw)
    if (not isinstance(record, dict) or _key(record.get('task_id')) != key
            or record.get('status') not in ('running', 'completed', 'unknown', 'expired')
            or record.get('delivery') not in ('pending', 'delivering', 'delivered', 'unknown', 'expired')
            or not isinstance(record.get('binding_sha256'), str) or len(record['binding_sha256']) != 64):
        raise ValueError('invalid background record')
    if record['status'] == 'expired':
        return record
    binding = record.get('binding')
    if (not {'owner_pid', 'owner_started_at', 'target', 'created_at', 'updated_at'} <= record.keys()
            or not isinstance(binding, dict) or hashlib.sha256(_encoded(binding).encode()).hexdigest() != record['binding_sha256']
            or any(not isinstance(record.get(field), str) or not record[field]
                   or binding.get(field) != record[field] for field in ('parent_session_id', 'profile_id', 'event_type'))
            or type(record.get('owner_pid')) is not int or record['owner_pid'] <= 0
            or record.get('owner_started_at') is not None and not _number(record['owner_started_at'])
            or any(not _number(record.get(field)) for field in ('created_at', 'updated_at'))
            or binding.get('owner_epoch') is not None and (type(binding['owner_epoch']) is not int or binding['owner_epoch'] < 0)):
        raise ValueError('invalid background binding or owner')
    if record['parent_session_id'] != 'messaging-background' and (not isinstance(record.get('target'), list) or len(record['target']) != 2):
        raise ValueError('invalid background target')
    for field in ('delivery_pid', 'delivery_epoch'):
        if record.get(field) is not None and (type(record[field]) is not int or record[field] <= 0):
            raise ValueError('invalid background delivery owner')
    if record.get('delivery_started_at') is not None and not _number(record['delivery_started_at']):
        raise ValueError('invalid background delivery time')
    if record['status'] == 'completed' and (not isinstance(record.get('payload'), dict)
            or record['payload'].get('task_id') != record['task_id'] or not isinstance(record['payload'].get('text'), str)):
        raise ValueError('invalid background payload')
    if record['status'] == 'completed' and record['parent_session_id'] == 'messaging-background' and (
            not isinstance(record['payload'].get('header', ''), str)
            or record['payload'].get('metadata') is not None and not isinstance(record['payload']['metadata'], dict)):
        raise ValueError('invalid background messaging payload')
    return record


def _records(conn):
    rows = conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?', (PREFIX, PREFIX + '\uffff')).fetchall()
    for key, raw in rows:
        try:
            yield key, _decode(key, raw)
        except (TypeError, ValueError, OverflowError):
            logger.warning('Background record %s retained for reconciliation', key)


def _save(conn, key, record):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, _encoded(record)))


def _check(record, task_id, binding):
    if record['task_id'] != task_id or record['binding_sha256'] != hashlib.sha256(_encoded(binding).encode()).hexdigest():
        raise ValueError('background task identity conflict')


def invalidate_target(conn, session_id):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 ('background-boundary:' + session_id, os.urandom(16).hex()))


def _target(conn, session_id):
    row = conn.execute('SELECT started_at FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row is None:
        return None
    boundary = conn.execute('SELECT value FROM state_meta WHERE key=?', ('background-boundary:' + session_id,)).fetchone()
    return [row[0], boundary[0] if boundary else None]


def _target_matches(conn, record):
    return (record['parent_session_id'] == 'messaging-background' or
            record.get('target') is not None and record['target'] == _target(conn, record['parent_session_id']))


def _epoch(conn, binding):
    if binding.get('owner_epoch') is not None:
        from hermes_state_runtime import _epoch as check_epoch
        check_epoch(conn, binding['owner_epoch'])


def _current_epoch(conn):
    row = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()
    return row[0] if row else None


def _reconcile(conn, record):
    if record['status'] == 'expired':
        return {key:record[key] for key in ('task_id', 'binding_sha256', 'status', 'delivery')}
    owner_epoch = record.get('binding', {}).get('owner_epoch')
    if record['status'] == 'running' and (not _owner_live(record) or
            owner_epoch is not None and owner_epoch != _current_epoch(conn)):
        record['status'] = 'unknown'
        record['updated_at'] = time.time()
    if record.get('delivery') == 'delivering':
        from cron.executions import _owner_is_live
        if (not _owner_is_live(record.get('delivery_pid', record['owner_pid']),
                              record.get('delivery_started_at', record['owner_started_at'])) or
                record.get('delivery_epoch') is not None and record['delivery_epoch'] != _current_epoch(conn)):
            record['delivery'] = 'unknown'
    return record


def _prune(conn, clock=time.time, expire_only=False):
    cutoff = clock() - RETENTION_SECONDS
    records = list(_records(conn))
    for key, value in records:
        if value['status'] == 'expired':
            _save(conn, key, _reconcile(conn, value))
        elif value['delivery'] == 'delivering':
            _reconcile(conn, value)
            _save(conn, key, value)
    terminal = sorted(((key, value) for key, value in records if value.get('status') == 'completed' and
                       value['delivery'] != 'delivering' and
                       (value.get('delivery') == 'delivered' or value['parent_session_id'] != 'messaging-background')),
                      key=lambda item:(item[1].get('updated_at', item[1].get('created_at', 0.0)), item[1].get('task_id', item[0])), reverse=True)
    acknowledged = 0
    for key, value in terminal:
        expired = value['created_at'] < cutoff
        if value['delivery'] == 'delivered':
            expired = expired or (not expire_only and acknowledged >= MAX_RESULTS)
            acknowledged += 1
        if expired:
            value['status'] = 'expired'
            _save(conn, key, _reconcile(conn, value))


def begin(db, task_id, binding):
    from gateway.status import get_process_start_time
    key = _key(task_id)
    if binding['profile_id'] != str(Path(db.db_path).parent.resolve()):
        raise ValueError('background profile identity conflict')
    def write(conn):
        record = _load(conn, key)
        if record is not None:
            _check(record, task_id, binding)
            record = _reconcile(conn, record)
            _save(conn, key, record)
            return record
        _epoch(conn, binding)
        target = _target(conn, binding['parent_session_id'])
        if target is None and binding['parent_session_id'] != 'messaging-background':
            raise ValueError('background parent identity missing')
        created = time.time()
        _prune(conn, clock=lambda: created, expire_only=True)
        count = conn.execute('SELECT COUNT(*) FROM state_meta WHERE key>=? AND key<?', (PREFIX, PREFIX + '\uffff')).fetchone()[0]
        if count >= MAX_IDENTITIES:
            raise ValueError('background identity capacity reached; preserve existing results and reconcile before new work')
        record = {'task_id':task_id, 'binding_sha256':hashlib.sha256(_encoded(binding).encode()).hexdigest(),
                  'parent_session_id':binding['parent_session_id'], 'event_type':binding['event_type'],
                  'profile_id':binding['profile_id'], 'binding':binding, 'target':target,
                  'status':'running', 'delivery':'pending',
                  'owner_pid':os.getpid(), 'owner_started_at':get_process_start_time(os.getpid()),
                  'created_at':created, 'updated_at':created}
        _save(conn, key, record)
        return dict(record, claimed=True)
    return db._execute_write(write)


def complete(db, task_id, binding, payload):
    from gateway.status import get_process_start_time
    from gateway.session_results import _redacted
    payload = _redacted(payload)
    def write(conn):
        key = _key(task_id)
        record = _load(conn, key)
        if record is None:
            raise ValueError('background task identity missing')
        _check(record, task_id, binding)
        if record['status'] == 'expired':
            raise ValueError('background execution unknown; do not replay')
        if record['status'] == 'completed':
            if record['payload'] != payload:
                raise ValueError('background result conflict')
            return record
        _epoch(conn, binding)
        if not _target_matches(conn, record):
            raise ValueError('background parent identity conflict')
        if record['status'] != 'running':
            raise ValueError('background execution unknown; do not replay')
        if (record['owner_pid'], record['owner_started_at']) != (os.getpid(), get_process_start_time(os.getpid())):
            raise ValueError('background owner identity conflict')
        record.update(status='completed', payload=payload, updated_at=time.time())
        _save(conn, key, record)
        _prune(conn)
        return record
    return db._execute_write(write)


def results(db, parent_session_id):
    def read(conn):
        _prune(conn, clock=time.time, expire_only=True)
        values = []
        for key, record in _records(conn):
            if record['status'] == 'expired':
                continue
            if (record['parent_session_id'] != parent_session_id or
                    record['profile_id'] != str(Path(db.db_path).parent.resolve()) or not _target_matches(conn, record)):
                continue
            previous = _encoded(record)
            _reconcile(conn, record)
            if _encoded(record) != previous:
                _save(conn, key, record)
            if record['status'] == 'completed':
                values.append((record['updated_at'], {'event_type':record['event_type'], 'payload':record['payload']}))
            elif record['status'] == 'unknown':
                values.append((record['updated_at'], {'event_type':record['event_type'], 'payload':{
                    'task_id':record['task_id'], 'text':'error: unknown_execution; reconcile existing artifacts before retrying'}}))
        return [value for _, value in sorted(values, key=lambda item:item[0])]
    return db._execute_write(read)


def pending_deliveries(db):
    def read(conn):
        _prune(conn, clock=time.time, expire_only=True)
        pending = []
        for key, record in _records(conn):
            record = _reconcile(conn, record)
            _save(conn, key, record)
            if record['status'] == 'expired':
                continue
            if (record['profile_id'] == str(Path(db.db_path).parent.resolve()) and
                    record['status'] == 'completed' and record['delivery'] == 'pending' and _target_matches(conn, record)):
                pending.append(record)
        return pending
    return db._execute_write(read)


def claim_delivery(db, task_id):
    from gateway.status import get_process_start_time
    def write(conn):
        key = _key(task_id)
        record = _load(conn, key)
        if record is None or record['status'] != 'completed' or record['delivery'] != 'pending':
            return False
        if record['profile_id'] != str(Path(db.db_path).parent.resolve()) or not _target_matches(conn, record):
            return False
        record.update(delivery='delivering', delivery_pid=os.getpid(),
                      delivery_started_at=get_process_start_time(os.getpid()), delivery_epoch=_current_epoch(conn))
        _save(conn, key, record)
        return True
    return db._execute_write(write)


def finish_delivery(db, task_id, outcome):
    from gateway.status import get_process_start_time
    if outcome not in ('delivered', 'unknown'):
        raise ValueError('invalid background delivery outcome')
    def write(conn):
        key = _key(task_id)
        record = _load(conn, key)
        if record is None or record['delivery'] != 'delivering':
            raise ValueError('background delivery claim missing')
        if (record.get('delivery_pid'), record.get('delivery_started_at')) != (os.getpid(), get_process_start_time(os.getpid())):
            raise ValueError('background delivery owner identity conflict')
        if record.get('delivery_epoch') is not None and record['delivery_epoch'] != _current_epoch(conn):
            raise ValueError('background delivery owner epoch conflict')
        record['delivery'] = outcome
        record['updated_at'] = time.time()
        _save(conn, key, record)
    db._execute_write(write)
