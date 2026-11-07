from copy import deepcopy
import json
from pathlib import Path
import re

FIELD = '_pending_final_identity_v1'


def from_row(row, binding):
    payload = row.get('payload') or json.loads(row['payload_json'])
    ingress = payload.get('native_text_v1', {}).get('ingress_sha256')
    if ingress is not None and (not isinstance(ingress, str) or not re.fullmatch('[0-9a-f]{64}', ingress)):
        raise RuntimeError('pending final ingress identity invalid')
    return {'version': 1, 'binding': deepcopy(binding), 'ingress_sha256': ingress}


def read(conn, home, binding):
    from gateway.pending_final import PREFIX, _binding, _check_descriptor, _json
    from hermes_state_terminal import RESULT_PREFIX, load_result
    try:
        if binding['profile_home'] != str(Path(home).resolve()):
            raise ValueError('home mismatch')
        admission = binding['admission_id']
        result = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission,)).fetchone()
        if result is None:
            return None
        stored = load_result(result[0])
        if not isinstance(stored, dict):
            raise ValueError('invalid terminal result')
        witness = stored.get(FIELD)
        if witness is None:
            return None
        if (not isinstance(witness, dict) or set(witness) != {'version', 'binding', 'ingress_sha256'}
                or type(witness['version']) is not int or witness['version'] != 1
                or _json(witness['binding']) != _json(binding)):
            raise ValueError('original binding mismatch')
        row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission,)).fetchone()
        if row is None or row['status'] != 'terminal' or row['outcome'] != 'completed':
            raise ValueError('terminal admission unavailable')
        current = _binding(dict(row), Path(home) / 'state.db')
        if current is None or _json(current) != _json(binding) or _json(from_row(dict(row), current)) != _json(witness):
            raise ValueError('current admission changed')
        raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + admission,)).fetchone()
        if raw is None:
            raise ValueError('final seal unavailable')
        descriptor = json.loads(raw[0])
        _check_descriptor(PREFIX + admission, descriptor)
        if (type(descriptor.get('identity_witness_version')) is not int or descriptor['identity_witness_version'] != 1
                or _json(descriptor['binding']) != _json(binding)):
            raise ValueError('sealed binding changed')
        return deepcopy(witness)
    except (KeyError, TypeError, ValueError, OverflowError, RuntimeError) as exc:
        raise ValueError('pending final identity witness invalid') from exc
