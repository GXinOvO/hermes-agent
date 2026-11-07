"""Transactional pending-result slots, shared by ingress and worker assignment."""
import json
from hermes_state_runtime import RuntimeStoreError, _admission, _epoch

PREFIX = 'gateway.managed_result_slot.v1.'


def release_slot(conn, admission_id):
    conn.execute('DELETE FROM state_meta WHERE key=?', (PREFIX + admission_id,))


def _slots(conn, *, epoch, profile_id):
    from hermes_state_managed_results import PREFIX as results_prefix, owner_alive
    active = set()
    for key, encoded in conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall():
        try:
            slot = json.loads(encoded)
            admission = _admission(conn, key[len(PREFIX):])
            if (slot['profile_id'] != profile_id or type(slot['epoch']) is not int
                    or type(slot['generation']) is not int or not isinstance(slot['owner'], dict)
                    or type(slot['owner'].get('pid')) is not int or slot['owner']['pid'] <= 0
                    or type(slot['owner'].get('birth')) not in (int, float)):
                raise RuntimeStoreError('managed_result_capacity')
            current = (slot['epoch'] == epoch == admission['owner_epoch']
                       and slot['generation'] == admission['generation']
                       and admission['status'] == 'started')
            live_owner = owner_alive(slot['owner'])
        except (RuntimeStoreError, KeyError, TypeError, ValueError):
            # An unreadable reservation is not evidence that capacity is available.
            raise RuntimeStoreError('managed_result_capacity') from None
        sealed = conn.execute('SELECT 1 FROM state_meta WHERE key=?',
                              (results_prefix + admission['admission_id'],)).fetchone()
        if sealed or admission['status'] == 'terminal' or (not current and not live_owner):
            release_slot(conn, admission['admission_id'])
        else:
            active.add(admission['admission_id'])
    return active


def reserve_slot(conn, *, admission, epoch, profile_id):
    from hermes_state_managed_results import MAX_BYTES, MAX_ROWS, PREFIX as results_prefix, _expire, process_identity
    _epoch(conn, epoch)
    if admission['status'] != 'started' or admission['owner_epoch'] != epoch:
        raise RuntimeStoreError('stale_generation')
    # Only the existing explicit TTL retires pending results; pressure never does.
    _expire(conn, profile_id=profile_id, epoch=epoch)
    active = _slots(conn, epoch=epoch, profile_id=profile_id)
    seals = conn.execute('SELECT key,length(CAST(value AS BLOB)) FROM state_meta WHERE key LIKE ?',
                         (results_prefix + '%',)).fetchall()
    sealed = {row[0][len(results_prefix):] for row in seals}
    # A pre-upgrade worker has no slot. It still owns one until settlement or owner loss.
    legacy = {row[0] for row in conn.execute(
        "SELECT a.admission_id FROM session_admissions a JOIN worker_executions w "
        "ON w.execution_id='admission-worker:'||a.admission_id "
        "WHERE a.status='started' AND a.owner_epoch=? AND w.owner_epoch=? "
        "AND w.status IN ('registered','running','terminal')", (epoch, epoch))}
    occupied = sealed | active | legacy
    if admission['admission_id'] in sealed or admission['admission_id'] in active:
        return
    if len(occupied - {admission['admission_id']}) >= MAX_ROWS or sum(row[1] for row in seals) >= MAX_BYTES:
        raise RuntimeStoreError('managed_result_capacity')
    slot = {'epoch': epoch, 'generation': admission['generation'], 'profile_id': profile_id,
            'owner': process_identity()}
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                 (PREFIX + admission['admission_id'], json.dumps(slot, sort_keys=True)))


def reserve_capacity(authority, admission_id):
    authority._require_admission_open()
    def write(conn):
        reserve_slot(conn, admission=_admission(conn, admission_id), epoch=authority.epoch,
                     profile_id=authority.profile_id)
    authority.db._execute_write(write)


def release_capacity(authority, admission_id):
    def write(conn):
        _epoch(conn, authority.epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + admission_id,)).fetchone()
        if row is not None and json.loads(row[0])['epoch'] == authority.epoch:
            release_slot(conn, admission_id)
    authority.db._execute_write(write)
