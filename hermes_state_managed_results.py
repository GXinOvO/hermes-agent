"""Owner-validated managed results, stored in SessionDB's existing metadata ledger.

A seal is pending evidence. Only the authenticated execution.finish transaction adds
completion evidence. Neither a pipe frame nor a terminal execution alone is sufficient.
"""
import json
import os
import time
from copy import deepcopy

from gateway.session_admission import admission_fingerprint
from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, _json, _session

PREFIX = 'managed.result.v1:'
MAX_ROWS = 64
MAX_BYTES = 64 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600
BOUNDARY_FIELDS = ('started_at', 'runtime_generation', 'parent_session_id', 'end_reason', 'model_config')
ADMISSION_FIELDS = ('admission_id', 'request_id', 'principal_id', 'target_session_id', 'generation',
                    'owner_epoch', 'payload_digest', 'intent', 'lineage_json')


def process_identity():
    import psutil
    from hermes_cli.process_identity import worker_birth
    return {'pid': os.getpid(), 'birth': worker_birth(psutil.Process())}


def owner_alive(identity):
    import psutil
    from hermes_cli.process_identity import worker_birth
    try:
        process = psutil.Process(identity['pid'])
        return worker_birth(process) == identity['birth'] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # uncertainty never authorizes takeover


def _boundary(conn, logical, physical):
    return {sid: admission_fingerprint(canonical_target=sid,
                payload={key: _session(conn, sid)[key] for key in BOUNDARY_FIELDS})
            for sid in {logical, physical}}


def _policy(conn, logical):
    from hermes_state_local import POLICY_PREFIX
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + logical,)).fetchone()
    return admission_fingerprint(canonical_target=logical, payload=row[0]) if row else None


def _proof(body):
    return admission_fingerprint(canonical_target=body['profile_id'], payload=body)


def _load(conn, admission_id):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + admission_id,)).fetchone()
    if row is None:
        raise RuntimeStoreError('unsealed_result')
    value = json.loads(row[0])
    if (not isinstance(value, dict) or set(value) != {'body', 'proof', 'completion'}
            or not isinstance(value['body'], dict) or value['proof'] != _proof(value['body'])
            or value['body'].get('version') != 2):
        raise RuntimeStoreError('invalid_managed_result')
    return value


def _expire(conn, *, profile_id, epoch):
    cutoff = time.time() - RETENTION_SECONDS
    expired = 0
    for key, in conn.execute('SELECT key FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall():
        try:
            body = _load(conn, key[len(PREFIX):])['body']
            admission = body['admission']
            eligible = (body['profile_id'] == profile_id and type(body['created_at']) in (int, float)
                and body['created_at'] < cutoff and admission['owner_epoch'] < epoch
                and admission['admission_id'] == key[len(PREFIX):] and not owner_alive(body['owner']))
        except (RuntimeStoreError, KeyError, TypeError, ValueError, OverflowError):
            continue
        if not eligible:
            continue
        conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=? AND status='started' AND owner_epoch<?",
                     (admission['admission_id'], epoch))
        conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
        expired += 1
    return expired


def expire_results(db, *, profile_id, epoch):
    def write(conn):
        _epoch(conn, epoch)
        return _expire(conn, profile_id=profile_id, epoch=epoch)
    return db._execute_write(write)


def _binding(conn, body, worker, *, completed=False):
    row = _admission(conn, body['admission']['admission_id'])
    expected = body['admission']
    if ({key: row[key] for key in ADMISSION_FIELDS} != expected
            or admission_fingerprint(canonical_target=row['admission_id'], payload=row['payload_json']) != body['input_hash']
            or worker['execution_id'] != 'admission-worker:' + row['admission_id']
            or worker['owner_epoch'] != expected['owner_epoch'] or worker['generation'] != expected['generation']
            or worker['session_id'] != body['physical_target']
            or _session(conn, row['target_session_id'])['runtime_generation'] != row['generation']
            or _boundary(conn, row['target_session_id'], worker['session_id']) != body['boundary']
            or _policy(conn, row['target_session_id']) != body['policy']):
        raise RuntimeStoreError('managed_result_identity_drift')
    if completed and (worker['status'] != 'terminal' or row['status'] not in ('started', 'unknown')):
        raise RuntimeStoreError('uncompleted_result')
    return row


def seal_result(authority, *, row, scope, raw_result, captured, stop_cause=None):
    """Private owner producer seam; no worker RPC accepts a result payload."""
    from agent.managed_worker import accept_result
    accepted, usage = accept_result(raw_result)
    if captured['usage'] != usage or {k: v for k, v in captured['result'].items()
                                     if k != 'local_viewer'} != accepted:
        raise RuntimeStoreError('invalid_managed_result')
    from gateway.session_results import _redacted
    captured = deepcopy(captured)
    if captured['result'].get('interrupted') and stop_cause:
        captured['result']['interrupt_message'] = stop_cause
    captured = _redacted(captured)
    def write(conn):
        _epoch(conn, authority.epoch)
        current = _admission(conn, row['admission_id'])
        worker = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                              (scope['execution_id'],)).fetchone()
        if (current['status'] != 'started' or current['owner_epoch'] != authority.epoch
                or current['generation'] != row['generation'] or worker is None
                or worker['status'] not in ('registered', 'running')
                or scope['epoch'] != authority.epoch or scope['profile_id'] != authority.profile_id
                or scope['generation'] != current['generation']):
            raise RuntimeStoreError('stale_generation')
        body = {'version': 2, 'profile_id': authority.profile_id, 'owner': process_identity(), 'created_at': time.time(),
                'admission': {key: current[key] for key in ADMISSION_FIELDS},
                'input_hash': admission_fingerprint(canonical_target=current['admission_id'], payload=current['payload_json']),
                'execution_id': scope['execution_id'], 'physical_target': worker['session_id'],
                'boundary': _boundary(conn, current['target_session_id'], worker['session_id']),
                'policy': _policy(conn, current['target_session_id']),
                'captured': captured}
        _binding(conn, body, worker)
        encoded = _json({'body': body, 'proof': _proof(body), 'completion': None})
        # Admission pressure only gates new execution. An authenticated current or
        # pre-upgrade worker must retain its accepted output even across that threshold.
        from hermes_state_managed_result_capacity import release_slot
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (PREFIX + row['admission_id'], encoded))
        release_slot(conn, row['admission_id'])
    return authority.db._execute_write(write)


def complete_result(conn, *, execution_id, session_id, epoch, generation, sequence):
    """Called inside the existing authenticated finish receipt's closing transaction."""
    if not execution_id.startswith('admission-worker:'):
        return
    admission_id = execution_id[len('admission-worker:'):]
    if not conn.execute('SELECT 1 FROM state_meta WHERE key=?', (PREFIX + admission_id,)).fetchone():
        return  # old workers and old empty finish receipts keep their original API
    value = _load(conn, admission_id)
    body = value['body']
    worker = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (execution_id,)).fetchone()
    _binding(conn, body, worker, completed=True)
    if (body['execution_id'] != execution_id or body['physical_target'] != session_id
            or body['admission']['owner_epoch'] != epoch or body['admission']['generation'] != generation):
        raise RuntimeStoreError('managed_result_identity_drift')
    value['completion'] = {'sequence': sequence, 'revisions': {
        sid: _session(conn, sid)['runtime_revision'] for sid in body['boundary']}}
    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(value), PREFIX + admission_id))


def retire_result(conn, admission_id):
    conn.execute('DELETE FROM state_meta WHERE key=?', (PREFIX + admission_id,))
    from hermes_state_managed_result_capacity import release_slot
    release_slot(conn, admission_id)


def consume_result(db, *, profile_id, epoch, admission_id):
    """One CAS transaction: validate evidence, move the old claim and settle exact output.

    Caller holds exclusive profile ownership. Liveness is checked again on the write
    connection; no model, script, worker adoption or external delivery is reachable here.
    """
    from gateway.session_results import prepare_result, close_discarded_turn
    from gateway.session_settlement_recovery import owner_stop_notice, gateway_refusal_boundary
    from hermes_state_runtime import settle_session_input
    with db._read_ctx() as conn:
        value = _load(conn, admission_id)
    body = value['body']
    if time.time() - body['created_at'] > RETENTION_SECONDS:
        raise RuntimeStoreError('managed_result_expired')
    captured = deepcopy(body['captured'])
    with db._read_ctx() as conn:
        admission = _admission(conn, admission_id)
        row = dict(admission, payload=json.loads(admission['payload_json']))
    response = captured['result']['final_response']
    outcome, stored = prepare_result(row, response, 'completed', captured)
    refused_plan = None
    if captured['result'].get('failure_reason') == 'content_policy_blocked':
        refused_plan = db._plan_refused_retention(body['physical_target'],
            captured['result'].get('refused_turn_boundary_row_id'))

    def claim(conn):
        current_seal = _load(conn, admission_id)
        if current_seal != value or profile_id != body['profile_id']:
            raise RuntimeStoreError('managed_result_identity_drift')
        old_epoch = body['admission']['owner_epoch']
        if old_epoch >= epoch or owner_alive(body['owner']):
            raise RuntimeStoreError('managed_result_owner_live')
        worker = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (body['execution_id'],)).fetchone()
        if worker is None:
            raise RuntimeStoreError('uncompleted_result')
        current = _binding(conn, body, worker, completed=True)
        completion = value['completion']
        if (not isinstance(completion, dict) or set(completion) != {'sequence', 'revisions'}
                or worker['last_sequence'] != completion['sequence']
                or completion['revisions'] != {sid: _session(conn, sid)['runtime_revision'] for sid in body['boundary']}):
            raise RuntimeStoreError('uncompleted_result')
        receipt = conn.execute('SELECT result_json FROM worker_receipts WHERE execution_id=? AND sequence=?',
                                (body['execution_id'], completion['sequence'])).fetchone()
        if receipt is None or json.loads(receipt[0]) != {'status': 'terminal'}:
            raise RuntimeStoreError('uncompleted_result')
        conn.execute("UPDATE session_admissions SET owner_epoch=?,status='started' WHERE admission_id=?",
                     (epoch, current['admission_id']))

    def terminal(conn, current):
        if refused_plan is not None:
            from agent.turn_failure_copy import CONTENT_REFUSED_NOTICE
            from hermes_state_rewind import retain_refused_in_transaction
            retain_refused_in_transaction(db, conn, body['physical_target'], CONTENT_REFUSED_NOTICE, refused_plan)
        notice = owner_stop_notice(captured, outcome, row)
        metadata = None
        if notice is None and (refusal := gateway_refusal_boundary(captured, outcome)) is not None:
            notice, metadata = refusal
        if notice is not None:
            close_discarded_turn(db, conn, row, notice, display_metadata=metadata)

    return settle_session_input(db, epoch=epoch, admission_id=admission_id, generation=row['generation'],
        outcome=outcome, result=stored, _recovery_write=claim, _terminal_write=terminal)
