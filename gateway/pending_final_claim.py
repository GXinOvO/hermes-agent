import hashlib
import json
import os
import time


def snapshot(value):
    fields = ('binding', 'target', 'adapter_profile', 'metadata', 'reply_to', 'response',
              'owner_pid', 'owner_started_at', 'created_at', 'turn_token', 'expired')
    context = {key: value.get(key) for key in fields}
    context['parts'] = {kind: {key: part.get(key) for key in ('payload_hash', 'content', 'extracted')}
                        for kind, part in value['parts'].items()}
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def matches(value, admission_id, expected=None):
    from gateway.pending_final import PREFIX, _check_descriptor
    try:
        _check_descriptor(PREFIX + admission_id, value)
        return expected is None or snapshot(value) == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def valid(conn, db, value, kind, *, expected=None):
    from pathlib import Path
    from gateway.delivery_ledger import _start_time
    from gateway.pending_final import RETENTION_SECONDS, _valid
    if not matches(value, value['binding']['admission_id'], expected):
        return False
    part = value['parts'].get(kind)
    if (part is None or part['state'] != 'attempting' or value.get('expired')
            or time.time() - value['created_at'] > RETENTION_SECONDS
            or part.get('claim_snapshot') != snapshot(value)
            or (part['owner_pid'], part['owner_started_at']) != (os.getpid(), _start_time(os.getpid()))):
        return False
    epoch = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()
    return (epoch is not None and part['delivery_epoch'] == epoch[0]
            and _valid(conn, value, str(Path(db.db_path).resolve().parent)))


def current(receipt, kind):
    from gateway.pending_final import _read
    db, admission, won, _state, claimed = receipt
    if not won:
        return False
    with db._read_ctx() as conn:
        value = _read(conn, admission)
        return value is not None and valid(conn, db, value, kind, expected=snapshot(claimed))
