import json
import uuid

from persistence.authority.vocabulary import admission_fingerprint
from persistence.conversation.messages import NewMessage
from persistence.sqlite.conversation._store.messages import _append_messages_in_transaction
from persistence.sqlite.conversation.messages import _message_dict
from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError, _json, _text


def _store_handles(db):
    store = getattr(db, "_store", db)
    return store, getattr(store, "_store", store)


def append_received_input(db, session_id, message, *, principal_id, request_id, active=True):
    for value in (session_id, principal_id, request_id):
        _text(value)
    metadata = {**message.get('display_metadata', {}), 'submission_id': request_id}
    message = {**message, 'display_metadata': metadata}
    payload = {'text': message['content'], 'legacy_input_v1': {
        'message_uid': message['message_uid'], 'received_at': message['timestamp']}}
    encoded = _json(payload)
    admission_id = uuid.uuid4().hex
    store, inner = _store_handles(db)
    holder = inner.holder()
    lineage = sorted({session_id, *store.lineage.lineage(session_id)})
    target = session_id if active else store.lineage.tip(session_id)
    marks = ','.join('?' for _ in lineage)
    fingerprints = {candidate: admission_fingerprint(canonical_target=candidate,
                                                     payload={'input': payload, 'intent': 'queue'})
                    for candidate in lineage}

    def write(conn):
        old = conn.execute(f'''SELECT payload_digest FROM session_admissions
            WHERE principal_id=? AND target_session_id IN ({marks}) AND request_id=?''',
            (principal_id, *lineage, request_id)).fetchone()
        if old is not None:
            if old['payload_digest'] not in fingerprints.values():
                raise RuntimeStoreError('admission_conflict', **ADMITTED_REQUEST_CONFLICT)
            row = conn.execute(f'''SELECT id FROM messages WHERE session_id IN ({marks}) AND message_uid=?
                AND json_extract(display_metadata,'$.submission_id')=? ORDER BY id LIMIT 1''',
                (*lineage, message['message_uid'], request_id)).fetchone()
            if row is None:
                raise RuntimeStoreError('admission_conflict')
            return row['id']
        conn.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,
            target_session_id,lineage_json,payload_json,payload_digest,intent,status,outcome)
            VALUES(?,?,?,?,?,?,?,'queue','terminal','recorded')''',
            (admission_id, request_id, principal_id, target, json.dumps([target]),
             encoded, fingerprints[session_id]))
        _append_messages_in_transaction(holder, conn, target, [_message_dict(NewMessage(
            role='user', content=message['content'], timestamp=message['timestamp'],
            message_uid=message['message_uid'], display_metadata=metadata))])
        row = conn.execute('SELECT id FROM messages WHERE session_id=? AND message_uid=? ORDER BY id DESC LIMIT 1',
                           (target, message['message_uid'])).fetchone()
        if row is None:
            raise RuntimeStoreError('admission_conflict')
        if active:
            columns = [info[1] for info in conn.execute('PRAGMA table_info(messages)') if info[1] != 'id']
            names = ', '.join(columns)
            conn.execute(f'INSERT INTO messages ({names}) SELECT {names} FROM messages WHERE id=?', (row['id'],))
            conn.execute('UPDATE messages SET active=0 WHERE id=last_insert_rowid()')
        else:
            conn.execute('UPDATE messages SET active=0 WHERE id=?', (row['id'],))
            conn.execute('UPDATE sessions SET message_count=MAX(message_count-1,0) WHERE id=?', (target,))
        return row['id']

    row_id = holder._execute_write(write, patience_s=holder._TRANSCRIPT_WRITE_PATIENCE_S)
    return row_id, metadata
