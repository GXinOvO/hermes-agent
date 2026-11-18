import json
import uuid

from persistence.authority.vocabulary import admission_fingerprint
from hermes_state_messages import _INSERT_MESSAGE_SQL
from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError, _json, _text


def _compression_lineage_ids(db, conn, session_id):
    ids, frontier = {session_id}, [session_id]
    for _ in range(1000):
        if not frontier:
            break
        next_ids = set(db._expand_compression_lineage(conn, frontier)) - ids
        if not next_ids:
            break
        ids.update(next_ids)
        frontier = list(next_ids)
    return ids


def append_received_input(db, session_id, message, *, principal_id, request_id, active=True):
    for value in (session_id, principal_id, request_id):
        _text(value)
    metadata = {**message.get('display_metadata', {}), 'submission_id': request_id}
    message = {**message, 'display_metadata': metadata}
    payload = {'text': message['content'], 'legacy_input_v1': {
        'message_uid': message['message_uid'], 'received_at': message['timestamp']}}
    encoded = _json(payload)
    admission_id = uuid.uuid4().hex

    def write(conn):
        target = db._compression_tip_on(conn, session_id) if not active else session_id
        db._check_transcript_write_guards(conn, target, None)
        params = db._message_row_params(target, 'user', message, None, message['timestamp'], keep_reasoning=False)
        lineage = _compression_lineage_ids(db, conn, session_id)
        fingerprint_payload = {'input': payload, 'intent': 'queue'}
        old = conn.execute('''SELECT payload_digest FROM session_admissions
            WHERE principal_id=? AND target_session_id IN ({}) AND request_id=?'''.format(
                ','.join('?' for _ in lineage)),
            (principal_id, *lineage, request_id)).fetchone()
        if old is not None:
            if not any(admission_fingerprint(canonical_target=candidate, payload=fingerprint_payload)
                       == old['payload_digest'] for candidate in lineage):
                raise RuntimeStoreError('admission_conflict', **ADMITTED_REQUEST_CONFLICT)
            row = conn.execute('''SELECT id FROM messages WHERE session_id IN ({}) AND message_uid=?
                AND json_extract(display_metadata,'$.submission_id')=? ORDER BY id LIMIT 1'''.format(
                    ','.join('?' for _ in lineage)),
                (*lineage, message['message_uid'], request_id)).fetchone()
            if row is None:
                raise RuntimeStoreError('admission_conflict')
            return row['id']
        digest = admission_fingerprint(canonical_target=session_id, payload=fingerprint_payload)
        conn.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,
            target_session_id,lineage_json,payload_json,payload_digest,intent,status,outcome)
            VALUES(?,?,?,?,?,?,?,'queue','terminal','recorded')''',
            (admission_id, request_id, principal_id, target, json.dumps([target]), encoded, digest))
        row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
        witness_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid if active else row_id
        conn.execute('UPDATE messages SET active=0 WHERE id=?', (witness_id,))
        db._bump_session_counters(conn, target, 1 if active else 0, 0, unit=False)
        return row_id

    row_id = db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
    return row_id, metadata
