"""A2A task records in the existing profile state DB; no transport or execution retry."""
from contextlib import closing
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time

from hermes_state import SessionDB

from . import protocol, security, task_bindings as tb

logger = logging.getLogger(__name__)
PREFIX = 'a2a.task.v1:'
MAX_IDENTITIES = 65536
RETENTION_SECONDS = 7 * 24 * 3600


class CapacityExceeded(RuntimeError):
    pass


def _key(task_id):
    return PREFIX + hashlib.sha256(task_id.encode()).hexdigest()


def _decode(key, raw, home):
    record = json.loads(raw)
    if isinstance(record, dict) and record.get('expired') is True:
        if (type(record.get('version')) is not int or record['version'] != 1
                or any(not isinstance(record.get(name), str) for name in tb.IDENTITY)
                or not record['task_id'] or not record['context_id'] or key != _key(record['task_id'])
                or record.get('profile_home') != str(Path(home).resolve())
                or record.get('state') not in protocol.TERMINAL_STATES
                or not isinstance(record.get('input_hash'), str) or len(record['input_hash']) != 64
                or any(c not in '0123456789abcdef' for c in record['input_hash'])):
            raise ValueError('invalid durable A2A replay fence')
        return record
    strings = ('task_id', 'context_id', 'peer', 'agent_slug', 'tenant', 'input_id',
               'state', 'reply', 'created_iso', 'push_url', 'push_config_id', 'profile_home')
    if (not isinstance(record, dict) or any(not isinstance(record.get(k), str) for k in strings)
            or not record['task_id'] or not record['context_id']
            or record['profile_home'] != str(Path(home).resolve()) or key != _key(record['task_id'])
            or record['state'] not in protocol.TERMINAL_STATES | {protocol.STATE_SUBMITTED, protocol.STATE_WORKING, protocol.STATE_INPUT_REQUIRED}
            or type(record.get('created_at')) not in (int, float)):
        raise ValueError('invalid durable A2A task')
    try:
        if not math.isfinite(record['created_at']):
            raise ValueError('invalid durable A2A timestamp')
    except OverflowError as exc:
        raise ValueError('invalid durable A2A timestamp') from exc
    return record


def _read(conn, task_id, home):
    key = _key(task_id)
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return _decode(key, row[0], home) if row else None


def _public_final(conn, record):
    from gateway import pending_final as pf
    result = record.get('durable_result_v1')
    if result is not None or 'public_target_v1' in record:
        binding = result['admission_binding'] if result is not None else record['public_target_v1']['binding']
        admission_id = result['admission_id'] if result is not None else binding['admission_id']
        value = pf._read(conn, admission_id)
        if value is None or value['binding'] != binding:
            raise RuntimeError('durable A2A final unavailable')
        return value
    if record['state'] not in {protocol.STATE_COMPLETED, protocol.STATE_INPUT_REQUIRED}:
        return None
    # Ordinary HUMAN completion uses TaskStore.complete, not strict publish. Its
    # original seal and independent witness already committed at native settlement.
    from gateway.pending_final_identity import FIELD
    from hermes_state_terminal import RESULT_PREFIX, load_result
    admissions = conn.execute('SELECT admission_id FROM session_admissions WHERE request_id=?',
                              (record['task_id'],)).fetchall()
    if len(admissions) > 1:
        raise RuntimeError('durable A2A final identity ambiguous')
    if admissions:
        admission_id = admissions[0][0]
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission_id,)).fetchone()
        stored = load_result(saved[0]) if saved else {}
        if isinstance(stored, dict) and FIELD in stored:
            value = pf._read(conn, admission_id)
            if value is None:
                raise RuntimeError('durable A2A final unavailable')
            return value
    # Deleted admissions move to native tombstones; their retained seal still
    # names the original task. Never manufacture a binding from today's session.
    rows = conn.execute("SELECT value FROM state_meta WHERE key LIKE ? AND "
                        "CASE WHEN json_valid(value) THEN json_extract(value,'$.reply_to') END=?",
                        (pf.PREFIX + '%', record['task_id'])).fetchall()
    if len(rows) > 1:
        raise RuntimeError('durable A2A final identity ambiguous')
    return json.loads(rows[0][0]) if rows else None


def _validate_result(conn, home, record):
    value = _public_final(conn, record)
    if value is None:
        return  # Legacy/forwarded completions have no native final identity to infer.
    from gateway import pending_final as pf
    from hermes_state_background import _target
    from hermes_state_common import _RESET_END_REASONS
    from hermes_state_mutation_retirement import retired_in
    binding = value['binding']
    pf._check_descriptor(pf.PREFIX + binding['admission_id'], value)
    if (binding['request_id'] != record['task_id'] or value['reply_to'] != record['task_id']
            or binding['source'].get('platform') != 'a2a'
            or binding['source']['chat_id'] != record['context_id']):
        raise RuntimeError('durable A2A final target mismatch')
    if 'input_hash' in record:
        tb.validate_admission(conn, home, record, binding)
    elif pf.read_identity_witness(conn, home, binding) is None:
        raise RuntimeError('durable A2A original final identity unavailable')
    session = conn.execute('SELECT end_reason FROM sessions WHERE id=?', (binding['session_id'],)).fetchone()
    # Claim eligibility also fences each turn's runtime_generation. Historical
    # reads instead follow the sealed destructive boundary, so later turns stay visible.
    if (session is None or session[0] in _RESET_END_REASONS
            or retired_in(conn, binding['session_id']) or value['target'] is None
            or value['target'] != _target(conn, binding['session_id'])):
        raise RuntimeError('durable A2A result target invalidated')
    if 'public_target_v1' in record and record['public_target_v1'] != _public_target(conn, value):
        raise RuntimeError('durable A2A conversation boundary changed')
    return value


def _public_target(conn, value):
    """Captured only while publishing a verified native seal, never by a reader."""
    binding = value['binding']
    session = conn.execute('SELECT source,session_key FROM sessions WHERE id=?', (binding['session_id'],)).fetchone()
    row = conn.execute('SELECT generation FROM conversation_generations WHERE source=? AND session_key=?',
                       tuple(session)).fetchone()
    return {'binding': binding, 'target': value['target'], 'conversation': [*session, row[0] if row else 0]}


def load(home, task_id=None):
    """Read existing state only. Public reads never initialize a writer/schema."""
    if not (Path(home) / 'state.db').exists():
        return []
    records = []
    with closing(SessionDB(Path(home) / 'state.db', read_only=True)) as db, db._read_ctx() as conn:
        query, args = ('SELECT key,value FROM state_meta WHERE key=?', (_key(task_id),)) if task_id else (
            'SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',))
        for key, raw in conn.execute(query, args):
            try:
                record = _decode(key, raw, home)
                if record.get('expired'):
                    continue
                if 'input_hash' in record:
                    tb.read_anchor(conn, record)
                _validate_result(conn, home, record)
                records.append(record)
            except (RuntimeError, ValueError, TypeError, KeyError, OverflowError):
                logger.warning('Damaged durable A2A task %s retained', key)
    return sorted(records, key=lambda record: record['created_at'])


def create(home, record, *, deduplicate=False, ingress=None):
    record = {**record, 'profile_home': str(Path(home).resolve())}
    if ingress:
        record['input_hash'] = ingress['input_hash']
    encoded = json.dumps(record, sort_keys=True)
    _decode(_key(record['task_id']), encoded, home)
    with closing(SessionDB(Path(home) / 'state.db')) as db:
        def write(conn):
            if deduplicate and record['input_id']:
                for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)):
                    try:
                        previous = _decode(key, raw, home)
                    except (ValueError, TypeError, KeyError, OverflowError):
                        continue
                    identity = ('context_id', 'peer', 'agent_slug', 'tenant', 'input_id')
                    if all(previous[k] == record[k] for k in identity):
                        if previous.get('expired'):
                            raise tb.InputConflict('A2A accepted result expired; reconcile without replaying the task')
                        anchor = tb.read_anchor(conn, previous)
                        if not ingress or anchor['input_hash'] != ingress['input_hash']:
                            raise tb.InputConflict('A2A messageId conflicts with accepted input')
                        try:
                            _validate_result(conn, home, previous)
                        except (RuntimeError, ValueError, TypeError, KeyError, OverflowError) as exc:
                            raise tb.InputConflict('A2A accepted result target invalidated; reconcile without replaying the task') from exc
                        return previous
            if _read(conn, record['task_id'], home) is not None:
                raise RuntimeError('durable A2A task already exists')
            count = conn.execute('SELECT COUNT(*) FROM state_meta WHERE key>=? AND key<?',
                                 (PREFIX, PREFIX + '\uffff')).fetchone()[0]
            if count >= MAX_IDENTITIES:
                raise CapacityExceeded('A2A identity capacity reached; reconcile existing results before new work')
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (_key(record['task_id']), encoded))
            if ingress:
                conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                             (tb.key(tb.INGRESS_PREFIX, record['task_id']), tb.encode(tb.anchor_for(record, ingress))))
            return record
        return db._execute_write(write)


def _sealed(conn, home, task_id):
    """Existing settlement records protect pending/uncertain finals inside the failure txn."""
    from gateway import pending_final as pf
    for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (pf.PREFIX + '%',)):
        try:
            value = json.loads(raw)
            pf._check_descriptor(key, value)
            if value['reply_to'] == task_id and pf._valid(conn, value, home):
                return True
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return False


def _confirmed(conn, home, record):
    from gateway import pending_final as pf
    result = record.get('durable_result_v1')
    if not isinstance(result, dict) or not result.get('admission_id'):
        return None
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (pf.PREFIX + result['admission_id'],)).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    pf._check_descriptor(pf.PREFIX + result['admission_id'], value)
    part = value['parts'].get('text') or {}
    public_hash = record.get('public_reply_sha256')
    current_hash = hashlib.sha256(record['reply'].encode()).hexdigest()
    if 'content' in part:
        reply_matches = record['reply'] == security.redact_outbound(part['content'])
    else:
        from gateway.pending_final_publication import read
        receipt = read(conn, home, value['binding'])
        expected = {'version': 1, 'platform': 'a2a', 'admission_id': result['admission_id'],
                    'task_id': record['task_id'], 'context_id': record['context_id'],
                    'payload_hash': result.get('payload_hash'), 'public_reply_sha256': current_hash}
        legacy_hash_match = (receipt is None and public_hash == current_hash
                             and pf._digest(record['reply']) == part.get('payload_hash'))
        reply_matches = (value.get('expired') and public_hash == current_hash
                         and (receipt == expected or legacy_hash_match))
    if (pf._valid(conn, value, home) and value['binding'] == result.get('admission_binding')
            and value['reply_to'] == record['task_id'] and part.get('state') == 'delivered'
            and part.get('attempts') == 1 and part.get('payload_hash') == result.get('payload_hash')
            and record['state'] == protocol.STATE_COMPLETED
            and reply_matches and (public_hash is None or public_hash == current_hash)):
        tb.validate_admission(conn, home, record, value['binding'])
        return part
    return None


def maintain(home):
    with closing(SessionDB(Path(home) / 'state.db')) as db:
        def write(conn):
            expired = []
            clock = time.time()
            for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?',
                                         (PREFIX, PREFIX + '\uffff')).fetchall():
                try:
                    record = _decode(key, raw, home)
                    if record.get('expired') or record['state'] not in protocol.TERMINAL_STATES:
                        continue
                    tb.read_anchor(conn, record)
                    part = _confirmed(conn, home, record)
                    if part is None:
                        continue
                    started = part.get('delivered_at', record.get('retention_started_at'))
                    if started is None:
                        record['retention_started_at'] = clock
                        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(record), key))
                    elif type(started) in (int, float) and math.isfinite(started) and 0 <= started <= clock - RETENTION_SECONDS:
                        fence = {name: record[name] for name in tb.IDENTITY}
                        fence.update(version=1, expired=True, input_hash=record['input_hash'], state=record['state'])
                        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(fence), key))
                        conn.execute('DELETE FROM state_meta WHERE key IN (?,?)',
                                     (tb.key(tb.INGRESS_PREFIX, record['task_id']), tb.key(tb.ADMISSION_PREFIX, record['task_id'])))
                        expired.append(record['task_id'])
                except (RuntimeError, ValueError, TypeError, KeyError, OverflowError):
                    logger.warning('A2A result %s retained for reconciliation', key)
            return expired
        return db._execute_write(write)


def update(home, task_id, fields, *, orphan=False):
    with closing(SessionDB(Path(home) / 'state.db')) as db:
        def write(conn):
            record = _read(conn, task_id, home)
            if record is None:
                raise RuntimeError('durable A2A task missing')
            if record.get('expired'):
                raise RuntimeError('durable A2A task expired')
            if 'state' in fields and record['state'] in protocol.TERMINAL_STATES:
                return record
            if orphan:
                from gateway.delivery_ledger import _owner_alive
                pid = record.get('owner_pid')
                if ((pid and _owner_alive(pid, record.get('owner_started_at')))
                        or _sealed(conn, home, task_id)):
                    return record
            record.update(fields)
            if fields.get('state') in {protocol.STATE_COMPLETED, protocol.STATE_INPUT_REQUIRED}:
                value = _validate_result(conn, home, record)
                if value is not None:
                    record['public_target_v1'] = _public_target(conn, value)
            encoded = json.dumps(record, sort_keys=True)
            _decode(_key(task_id), encoded, home)
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (encoded, _key(task_id)))
            return record
        return db._execute_write(write)


def _binding(conn, home, chat_id, content, reply_to, metadata, agents, *, publishing):
    from gateway import pending_final as pf
    from gateway.delivery_ledger import _start_time
    if not isinstance(reply_to, str) or not reply_to or not (metadata or {}).get('notify'):
        raise RuntimeError('durable A2A publication requires an exact final task')
    task = _read(conn, reply_to, home)
    if task is None or task.get('expired') or task['context_id'] != chat_id:
        raise RuntimeError('durable A2A task target mismatch')
    agent = agents.get(task['agent_slug'])
    if not agent or agent.get('tenant', '') != task['tenant'] or not agent.get('local', True):
        raise RuntimeError('durable A2A task scope mismatch')
    tb.read_anchor(conn, task)
    matches = []
    for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (pf.PREFIX + '%',)):
        try:
            value = json.loads(raw)
            pf._check_descriptor(key, value)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        binding, part = value['binding'], value['parts'].get('text')
        source = binding['source']
        if (value['reply_to'] != reply_to or value['metadata'] != metadata
                or binding['profile_home'] != str(Path(home).resolve())
                or source.get('platform') != 'a2a' or source.get('chat_id') != chat_id
                or not part or part.get('content') != content
                or part['state'] not in {'pending', 'attempting'} or value.get('expired')):
            continue
        if not pf._valid(conn, value, home):
            continue
        tb.validate_admission(conn, home, task, binding)
        if publishing and (part['state'] != 'attempting' or
                (part.get('owner_pid'), part.get('owner_started_at')) != (os.getpid(), _start_time(os.getpid()))
                or part.get('delivery_epoch') != conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()[0]):
            continue
        matches.append((binding, part))
    if len(matches) != 1:
        raise RuntimeError('durable A2A publication has no unique sealed result claim')
    binding, part = matches[0]
    result = {'admission_id': binding['admission_id'], 'request_id': binding['request_id'],
              'session_id': binding['session_id'], 'generation': binding['generation'],
              'route': binding['route'], 'payload_hash': part['payload_hash'],
              'task_id': reply_to, 'context_id': chat_id, 'agent_slug': task['agent_slug'], 'tenant': task['tenant'],
              'ingress_hash': task['input_hash'], 'admission_binding': binding}
    if task['state'] in protocol.TERMINAL_STATES and task.get('durable_result_v1') != result:
        raise RuntimeError('durable A2A task already settled to a different result')
    return task, result


def prepare(home, chat_id, content, reply_to, metadata, agents):
    with closing(SessionDB(Path(home) / 'state.db', read_only=True)) as db, db._read_ctx() as conn:
        _binding(conn, home, chat_id, content, reply_to, metadata, agents, publishing=False)


def _public_result(content):
    reply, state = security.redact_outbound(content), protocol.STATE_COMPLETED
    if reply.lstrip().upper().startswith(protocol.INPUT_REQUIRED_MARKER):
        reply, state = reply.lstrip()[len(protocol.INPUT_REQUIRED_MARKER):].strip(), protocol.STATE_INPUT_REQUIRED
    return reply, state


def verify_publication(conn, home, receipt, value):
    try:
        task = _read(conn, receipt['task_id'], home)
        if task is None or task.get('expired'):
            return False
        binding = value['binding']
        part = value['parts']['text']
        tb.validate_admission(conn, home, task, binding)
        reply, state = _public_result(part['content'])
        expected = {'version': 1, 'platform': 'a2a', 'admission_id': binding['admission_id'],
                    'task_id': task['task_id'], 'context_id': task['context_id'],
                    'payload_hash': part['payload_hash'],
                    'public_reply_sha256': hashlib.sha256(reply.encode()).hexdigest()}
        result = task.get('durable_result_v1')
        return (isinstance(result, dict) and type(receipt.get('version')) is int and receipt == expected
                and binding['source']['platform'] == 'a2a'
                and task['task_id'] == value['reply_to'] == binding['request_id']
                and task['context_id'] == binding['source']['chat_id']
                and task['state'] == state and task['reply'] == reply
                and task.get('public_reply_sha256') == expected['public_reply_sha256']
                and result.get('admission_binding') == binding
                and result.get('payload_hash') == part['payload_hash']
                and result.get('ingress_hash') == task['input_hash'])
    except (RuntimeError, ValueError, TypeError, KeyError, OverflowError):
        return False


def publish(home, chat_id, content, reply_to, metadata, agents, *, with_receipt=False):
    with closing(SessionDB(Path(home) / 'state.db')) as db:
        def write(conn):
            task, binding = _binding(conn, home, chat_id, content, reply_to, metadata, agents, publishing=True)
            if task['state'] not in protocol.TERMINAL_STATES:
                reply, state = _public_result(content)
                task.update(state=state, reply=reply, durable_result_v1=binding,
                            public_reply_sha256=hashlib.sha256(reply.encode()).hexdigest())
                value = _validate_result(conn, home, task)
                task['public_target_v1'] = _public_target(conn, value)
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(task, sort_keys=True), _key(reply_to)))
            if not with_receipt:
                return task
            from gateway.pending_final_publication import capture
            def verify(conn, receipt, value):
                return verify_publication(conn, home, receipt, value)
            receipt = capture(conn, home, binding['admission_id'], 'text', task_id=reply_to,
                              context_id=chat_id, public_reply=task['reply'], verify=verify)
            return task, receipt
        return db._execute_write(write)
