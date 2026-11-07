"""Plugin-owned immutable ingress anchors; admission authority stays in the gateway."""
import hashlib
import json
from pathlib import Path

INGRESS_PREFIX = 'a2a.ingress.v1:'
ADMISSION_PREFIX = 'a2a.admission.v1:'
IDENTITY = ('task_id', 'context_id', 'peer', 'agent_slug', 'tenant', 'input_id', 'profile_home')


class InputConflict(ValueError):
    """An accepted message identity cannot name different input."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def canonical_input(params, context_id):
    """Keep all JSON parts, metadata and configuration; normalize only protocol defaults.

    The transport's method/id are outside params. Missing role means user; legacy
    top-level contextId and the generated first-send context name the same context.
    Media is retained in this identity but is not a strict media delivery capability.
    """
    value = json.loads(encode(params))
    message = value.setdefault('message', {})
    if not isinstance(message, dict):
        raise ValueError('A2A message must be an object')
    message['contextId'] = context_id
    message.setdefault('role', 'ROLE_USER')
    value.pop('contextId', None)
    return value


def key(prefix, task_id):
    return prefix + hashlib.sha256(task_id.encode()).hexdigest()


def anchor_for(record, ingress):
    return {**{name: record[name] for name in IDENTITY}, **ingress, 'version': 1}


def read_anchor(conn, task):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key(INGRESS_PREFIX, task['task_id']),)).fetchone()
    if row is None:
        raise RuntimeError('durable A2A task has no trusted ingress anchor')
    anchor = json.loads(row[0])
    if (not isinstance(anchor, dict) or anchor.get('version') != 1
            or any(anchor.get(name) != task[name] for name in IDENTITY)
            or anchor.get('input_hash') != task.get('input_hash')
            or digest(anchor['input']) != anchor['input_hash']):
        raise RuntimeError('durable A2A task ingress identity changed')
    return anchor


def validate_admission(conn, home, task, binding):
    """Compare to ingress and the authentic admission, never task's current valid scope alone."""
    from gateway.pending_final import read_identity_witness
    anchor = read_anchor(conn, task)
    raw = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (binding['admission_id'],)).fetchone()
    payload = json.loads(raw['payload_json']) if raw else {}
    source = dict(binding['source'])
    source.setdefault('is_bot', False)
    expected_source = dict(anchor['source'])
    expected_source.setdefault('is_bot', False)
    if (binding['profile_home'] != str(Path(home).resolve())
            or binding['request_id'] != task['task_id']
            or source != expected_source or payload.get('text') != anchor['text']):
        raise RuntimeError('durable A2A admission does not match trusted ingress')
    witness = read_identity_witness(conn, home, binding)
    fingerprint = payload.get('native_text_v1', {}).get('ingress_sha256')
    if (witness is None or witness.get('ingress_sha256') != digest(anchor)
            or fingerprint != witness['ingress_sha256']):
        raise RuntimeError('durable A2A admission ingress fingerprint changed')
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key(ADMISSION_PREFIX, task['task_id']),)).fetchone()
    if row and json.loads(row[0]) != binding:
        raise RuntimeError('durable A2A original admission binding changed')
    return anchor
