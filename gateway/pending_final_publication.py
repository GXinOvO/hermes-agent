from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet

_FIELD = 'publication_receipt_v1'
_issued = WeakSet()


@dataclass(frozen=True, eq=False)
class _Publication:
    encoded: str
    snapshot: str
    verify: Callable
    database: tuple


def _database(conn, home):
    database = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
    path = Path(database).resolve()
    if not database or path != Path(home).resolve() / 'state.db':
        raise ValueError('pending final publication database changed')
    try:
        identity = path.stat()
    except OSError as exc:
        raise ValueError('pending final publication database unavailable') from exc
    return str(path), identity.st_dev, identity.st_ino


def check(value, kind):
    part = value['parts'][kind]
    if 'publication_receipt' not in part:
        return
    receipt = part['publication_receipt']
    binding = value['binding']
    source = binding['source']
    fields = {'version', 'platform', 'admission_id', 'task_id', 'context_id', 'payload_hash', 'public_reply_sha256'}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt['version']) is not int or receipt['version'] != 1
            or any(not isinstance(receipt[key], str) or not receipt[key] for key in fields - {'version'})
            or kind != 'text' or part['state'] != 'delivered' or part['attempts'] != 1
            or 'delivered_at' not in part
            or receipt['platform'] != source['platform'] or receipt['admission_id'] != binding['admission_id']
            or receipt['task_id'] != value['reply_to'] or receipt['task_id'] != binding['request_id']
            or receipt['context_id'] != source['chat_id'] or receipt['payload_hash'] != part['payload_hash']
            or any(len(receipt[key]) != 64 or any(c not in '0123456789abcdef' for c in receipt[key])
                   for key in ('payload_hash', 'public_reply_sha256'))):
        raise ValueError('pending final publication receipt invalid')


def capture(conn, home, admission_id, kind, *, task_id, context_id, public_reply, verify):
    from gateway.pending_final import _read, read_identity_witness
    from gateway.pending_final_claim import snapshot, valid
    db = SimpleNamespace(db_path=Path(home).resolve() / 'state.db')
    value = _read(conn, admission_id)
    database = _database(conn, home)
    if (not conn.in_transaction
            or not callable(verify) or not isinstance(public_reply, str)
            or value is None or not valid(conn, db, value, kind)
            or read_identity_witness(conn, home, value['binding']) is None):
        raise ValueError('pending final publication requires current transactional claim')
    binding = value['binding']
    receipt = {'version': 1, 'platform': binding['source']['platform'], 'admission_id': admission_id,
               'task_id': task_id, 'context_id': context_id, 'payload_hash': value['parts'][kind]['payload_hash'],
               'public_reply_sha256': hashlib.sha256(public_reply.encode()).hexdigest()}
    checked = deepcopy(value)
    checked['parts'][kind].update(state='delivered', delivered_at=0, publication_receipt=receipt)
    check(checked, kind)
    if verify(conn, deepcopy(receipt), deepcopy(value)) is not True:
        raise ValueError('pending final publication row does not match receipt')
    capability = _Publication(json.dumps(receipt, sort_keys=True), snapshot(value), verify, database)
    _issued.add(capability)
    return capability


def from_result(conn, value, kind, result):
    from gateway.pending_final import read_identity_witness
    from gateway.pending_final_claim import snapshot
    raw = getattr(result, 'raw_response', None)
    if not isinstance(raw, dict) or _FIELD not in raw:
        return None
    capability = raw[_FIELD]
    if (getattr(result, 'success', None) is not True or type(capability) is not _Publication
            or capability not in _issued or capability.snapshot != snapshot(value)
            or capability.database != _database(conn, value['binding']['profile_home'])
            or read_identity_witness(conn, value['binding']['profile_home'], value['binding']) is None):
        raise ValueError('pending final publication capability unavailable or changed')
    receipt = json.loads(capability.encoded)
    checked = deepcopy(value)
    checked['parts'][kind].update(state='delivered', delivered_at=0, publication_receipt=receipt)
    check(checked, kind)
    if (getattr(result, 'message_id', None) != receipt['task_id']
            or capability.verify(conn, deepcopy(receipt), deepcopy(value)) is not True):
        raise ValueError('pending final publication ACK does not match committed publication')
    return receipt


def read(conn, home, binding, kind='text'):
    from gateway.pending_final import PREFIX, _check_descriptor, _json, _read, _valid, read_identity_witness
    value = _read(conn, binding['admission_id'])
    if value is None:
        return None
    _check_descriptor(PREFIX + binding['admission_id'], value)
    part = value['parts'].get(kind, {})
    if 'publication_receipt' not in part:
        return None
    receipt = part['publication_receipt']
    _database(conn, home)
    if (_json(value['binding']) != _json(binding) or read_identity_witness(conn, home, binding) is None
            or not _valid(conn, value, home)):
        raise ValueError('pending final publication identity changed')
    return deepcopy(receipt)
