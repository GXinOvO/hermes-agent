from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path


def _descriptor(payload):
    local = payload.get('local_automation_v1')
    native = payload.get('native_text_v1')
    if isinstance(local, dict) and native is None:
        return local
    if local is None and isinstance(native, dict) and isinstance(native.get('automation'), dict):
        return native['automation']
    raise ValueError('strict Bot native admission payload unavailable')


def snapshot(db, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone()
    if row is None:
        raise ValueError('strict Bot native admission unavailable before receipt')
    payload = json.loads(row['payload_json'])
    _descriptor(payload).pop('sealed_cron_result_v1')
    payload.pop('text')
    return {'version': 1, 'payload': payload}


def _payload(row, params, receipt, *, retired):
    proof = receipt.get('native_admission_v1')
    if proof is None:
        if retired:
            raise ValueError('strict Bot retired admission lacks original payload evidence')
        return json.loads(row['payload_json'])
    if (not isinstance(proof, dict) or set(proof) != {'version', 'payload'}
            or type(proof['version']) is not int or proof['version'] != 1 or not isinstance(proof['payload'], dict)):
        raise ValueError('strict Bot native admission receipt invalid')
    payload = deepcopy(proof['payload'])
    descriptor = _descriptor(payload)
    if 'text' in payload or 'sealed_cron_result_v1' in descriptor:
        raise ValueError('strict Bot native admission template invalid')
    payload['text'] = params['message']
    descriptor['sealed_cron_result_v1'] = params['sealed_cron_result_v1']
    if not retired and payload != json.loads(row['payload_json']):
        raise ValueError('strict Bot native admission payload changed')
    return payload


def validate(home, params, receipt):
    from gateway.session_delivery_policy import consume_only
    from hermes_state import SessionDB
    from hermes_state_runtime import admission_fingerprint
    from hermes_state_terminal import terminal_admission
    home = Path(home).resolve()
    with closing(SessionDB(home / 'state.db', read_only=True)) as db, db._read_ctx() as conn:
        raw = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (receipt['admission_id'],)).fetchone()
        retired = raw is None
        row = dict(raw) if raw is not None else terminal_admission(conn, receipt['admission_id'])
        if row is None:
            raise ValueError('strict Bot receipt references missing native admission')
        payload = _payload(row, params, receipt, retired=retired)
        descriptor = _descriptor(payload)
        route = descriptor.get('route', (payload.get('native_text_v1') or {}).get('route'))
        sealed = params['sealed_cron_result_v1']
        if (not isinstance(route, str) or not route or row['principal_id'] != 'automation:' + route
                or row['admission_id'] != receipt['admission_id']
                or row['target_session_id'] != params['session_id'] or row['request_id'] != 'bot:' + params['id']
                or row['status'] not in {'queued', 'started', 'unknown', 'terminal'} or row['intent'] != 'queue'
                or retired and row['status'] != 'terminal'
                or payload.get('text') != params['message'] or descriptor.get('sealed_cron_result_v1') != sealed
                or descriptor.get('notification_category', 'result') != params.get('notification_category', 'result')
                or descriptor.get('turn_author') != params.get('author')
                or not consume_only({**row, 'payload': payload})
                or sealed['target_home'] != str(home) or sealed['target_session_id'] != row['target_session_id']
                or ('local_automation_v1' in payload and descriptor.get('target') != sealed['target_live_session_id'])
                or row['payload_digest'] != admission_fingerprint(canonical_target=row['target_session_id'],
                                                                 payload={'input': payload, 'intent': row['intent']})):
            raise ValueError('strict Bot native admission identity mismatch')
