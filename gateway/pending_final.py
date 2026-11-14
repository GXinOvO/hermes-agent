"""Settlement-owned exact finals; only a durable pending part is safe to recover.

The descriptor commits in the admission's terminal transaction. Its independent
CAS is the first send claim; an orphaned claim is unknown, including a claim made
before the first byte. Nothing is reconstructed from transcript or prompt text.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import time

logger = logging.getLogger(__name__)
PREFIX = 'gateway.pending_final.v1:'
_SEAL = object()
RETENTION_SECONDS = 7 * 24 * 3600


class DeliveryUncertain(RuntimeError):
    pass


class _Prepared(dict):
    """Runtime capability minted by the owner extractor, never model/result JSON."""
    seal = _SEAL


def prepared_value(value):
    return value if type(value) is _Prepared and value.seal is _SEAL else None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _binding(row, db_path):
    from gateway.session_delivery_policy import _row_policy
    row = dict(row)
    payload = row['payload'] if 'payload' in row else json.loads(row['payload_json'])
    if not isinstance(payload, dict):
        raise ValueError('invalid final admission payload')
    env = payload.get('native_text_v1', {})
    if (not isinstance(env, dict)
            or any(name in env and not isinstance(env[name], dict) for name in ('source', 'automation'))
            or 'local_automation_v1' in payload and not isinstance(payload['local_automation_v1'], dict)):
        raise ValueError('invalid final admission envelope')
    policy = _row_policy({**dict(row), 'payload':payload})
    if policy == 'legacy_unknown' or not env.get('source'):
        return None
    return {'version':1, 'profile_home':str(Path(db_path).resolve().parent),
            'admission_id':row['admission_id'], 'session_id':row['target_session_id'],
            'generation':row['generation'], 'owner_epoch':row['owner_epoch'],
            'principal_id':row['principal_id'], 'request_id':row['request_id'],
            'input_hash':_digest(payload), 'route':env['route'], 'policy':policy,
            'source':deepcopy(env['source'])}


async def prepare(authority, row, event, route, response):
    """Extract exact outgoing text/media before settlement, without a transport call."""
    binding = await asyncio.to_thread(_binding, row, authority.db.db_path)
    if binding is None or binding['route'] != route:
        return None
    adapter = authority.runner._adapter_for_source(event.source)
    if adapter is None:
        raise RuntimeError('pending final has no exact delivery adapter')
    from gateway.platforms.base import _reply_anchor_for_event, _thread_metadata_for_event, _mark_notify_metadata
    response, ttl = adapter._unwrap_ephemeral(response)
    from gateway.warning_notifications import diagnostic_wake_muted
    with adapter._media_delivery_scope(event.source):
        if diagnostic_wake_muted(event):
            response = None
    extracted = await adapter._extract_response_content(response or '', event, route, is_ephemeral_response=ttl > 0)
    parts = {}
    if extracted.text_content.strip():
        parts['text'] = {'state':'pending', 'content':extracted.text_content, 'attempts':0}
    if extracted.images or extracted.media_files or extracted.local_files:
        from gateway.final_delivery import _attachments_content
        content = await asyncio.to_thread(_attachments_content, extracted)
        parts['attachments'] = {'state':'pending', 'content':content, 'attempts':0,
            'extracted':{'images':extracted.images, 'media_files':extracted.media_files,
                         'local_files':extracted.local_files,
                         'force_document_attachments':extracted.force_document_attachments,
                         'text_content':'', 'pre_extract':extracted.pre_extract}}

    return _Prepared({'binding':binding, 'parts':parts, 'response':response,
            'turn_token':getattr(event, '_gateway_active_turn_token', None),
            'reply_to':_reply_anchor_for_event(event), 'metadata':_mark_notify_metadata(_thread_metadata_for_event(event)) or {},
            'adapter_profile':getattr(adapter, '_owner_profile', None)})


async def capture_result(authority, row, event, route, response, result):
    if row['payload'].get('native_text_v1'):
        descriptor = await prepare(authority, row, event, route, response)
        if descriptor is not None:
            return {**result, '_pending_final_v1':descriptor}
    return result


def persist(conn, row, prepared, *, db_path):
    """Owner's terminal transaction effect. Any descriptor error rolls settlement back."""
    if prepared is None:
        return
    if prepared_value(prepared) is None:
        raise RuntimeError('pending final runtime capability missing')
    expected = _binding(row, db_path)
    if prepared.get('binding') != expected or expected is None:
        raise RuntimeError('pending final admission binding changed')
    from gateway.delivery_ledger import _start_time
    value = deepcopy(prepared)
    value.update(owner_pid=os.getpid(), owner_started_at=_start_time(os.getpid()), created_at=time.time())
    from hermes_state_background import _target
    value['target'] = _target(conn, expected['session_id'])
    for kind, part in value['parts'].items():
        if kind not in {'text', 'attachments'} or part['state'] != 'pending' or part['attempts'] != 0:
            raise RuntimeError('pending final invalid initial part')
        part['payload_hash'] = _digest(part['content'])
    key = PREFIX + row['admission_id']
    previous = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    if previous is not None:
        raise RuntimeError('pending final already sealed')
    from gateway.pending_final_identity import from_row
    witness = from_row(dict(row), expected)
    value['identity_witness_version'] = 1
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, _json(value)))
    return witness


def read_identity_witness(conn, home, binding):
    from gateway.pending_final_identity import read
    return read(conn, home, binding)


def _check_descriptor(key, value):
    import math
    from gateway.session import SessionSource
    required = {'binding', 'parts', 'metadata', 'reply_to', 'adapter_profile',
                'owner_pid', 'owner_started_at', 'created_at', 'target'}
    binding_fields = {'version', 'profile_home', 'admission_id', 'session_id', 'generation',
                      'owner_epoch', 'principal_id', 'request_id', 'input_hash', 'route', 'policy', 'source'}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError('incomplete final descriptor')
    binding = value['binding']
    if not isinstance(binding, dict) or not binding_fields <= set(binding):
        raise ValueError('incomplete final binding')
    if (type(binding['version']) is not int or binding['version'] != 1
            or key != PREFIX + binding['admission_id']
            or any(not isinstance(binding[name], str) or not binding[name] for name in
                   ('profile_home', 'admission_id', 'session_id', 'principal_id', 'request_id', 'input_hash', 'route'))
            or any(type(binding[name]) is not int or binding[name] < 0 for name in ('generation', 'owner_epoch'))
            or binding['policy'] not in {'strict_automation_v1', 'human_reply_v1'}):
        raise ValueError('invalid final binding')
    source = binding['source']
    if not isinstance(source, dict) or not isinstance(source.get('chat_id'), str) or not source['chat_id']:
        raise ValueError('invalid final source')
    SessionSource.from_dict(source)
    if (not isinstance(value['metadata'], dict)
            or value['reply_to'] is not None and not isinstance(value['reply_to'], str)
            or value['adapter_profile'] is not None and not isinstance(value['adapter_profile'], str)
            or type(value['owner_pid']) is not int or value['owner_pid'] <= 0
            or value['owner_started_at'] is not None and type(value['owner_started_at']) is not int
            or type(value['created_at']) not in (int, float) or not math.isfinite(value['created_at'])
            or type(value.get('expired', False)) is not bool
            or not isinstance(value['parts'], dict) or set(value['parts']) - {'text', 'attachments'}):
        raise ValueError('invalid final delivery context')
    for kind, part in value['parts'].items():
        _check_part(kind, part, expired=value.get('expired', False))
        from gateway.pending_final_publication import check
        check(value, kind)


def _check_part(kind, part, *, expired):
    if (not isinstance(part, dict) or not {'state', 'attempts', 'payload_hash'} <= set(part)
            or part['state'] not in {'pending', 'attempting', 'unknown', 'delivered', 'expired'}
            or type(part['attempts']) is not int or not 0 <= part['attempts'] <= 1
            or not isinstance(part['payload_hash'], str) or len(part['payload_hash']) != 64):
        raise ValueError('invalid final part')
    if 'delivered_at' in part:
        import math
        stamp = part['delivered_at']
        try:
            if (part['state'] != 'delivered' or type(stamp) not in (int, float)
                    or stamp < 0 or not math.isfinite(stamp)):
                raise ValueError('invalid final ACK time')
        except OverflowError as exc:
            raise ValueError('invalid final ACK time') from exc
    if part['state'] == 'attempting' and (type(part.get('owner_pid')) is not int
            or part['owner_pid'] <= 0 or 'owner_started_at' not in part
            or part['owner_started_at'] is not None and type(part['owner_started_at']) is not int
            or type(part.get('delivery_epoch')) is not int):
        raise ValueError('invalid final attempt owner')
    if not expired and (not isinstance(part.get('content'), str) or _digest(part['content']) != part['payload_hash']):
        raise ValueError('invalid final payload')
    if not expired and kind == 'attachments':
        from gateway.platforms.base import _ExtractedResponse
        if not isinstance(part.get('extracted'), dict):
            raise ValueError('invalid final attachments')
        _ExtractedResponse(**part['extracted'])


def _records(conn):
    records = []
    for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX+'%',)):
        try:
            value = json.loads(raw)
            _check_descriptor(key, value)
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.warning('Unreadable pending final %s retained for reconciliation', key)
            continue
        records.append((key, value))
    return records


def pending(db):
    with db._read_ctx() as conn:
        return [value for _key, value in _records(conn)]


def reconcile_orphans(db):
    """Unknown is a send-stage fact, independent of current transport availability."""
    from gateway.delivery_ledger import _owner_alive
    def write(conn):
        for key, value in _records(conn):
            changed = False
            for part in value['parts'].values():
                if part['state'] == 'attempting' and not _owner_alive(part['owner_pid'], part['owner_started_at']):
                    part.update(state='unknown', error='owner exited without durable ACK')
                    changed = True
            live = any(part['state'] == 'attempting' and _owner_alive(part['owner_pid'], part['owner_started_at'])
                       for part in value['parts'].values())
            live = live or (any(part['state'] == 'pending' for part in value['parts'].values()) and
                            _owner_alive(value['owner_pid'], value['owner_started_at']))
            if not live and not value.get('expired') and time.time() - value['created_at'] > RETENTION_SECONDS:
                for part in value['parts'].values():
                    part.pop('content', None)
                    part.pop('extracted', None)
                value.pop('response', None)
                value['expired'] = True
                changed = True
            if changed:
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(value), key))
    db._execute_write(write)


def settled_for_session(runner, session_id, token):
    from gateway.session_authorities import all_authorities
    return any(value['binding']['session_id'] == session_id and value.get('turn_token') == token
               for authority in all_authorities(runner) for value in pending(authority.db))


def _valid(conn, value, home):
    from hermes_state_mutation_retirement import retired_in
    binding = value['binding']
    raw = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (binding['admission_id'],)).fetchone()
    if raw is None or raw['status'] != 'terminal' or raw['outcome'] != 'completed':
        return False
    try:
        if read_identity_witness(conn, home, binding) is None and 'identity_witness_version' in value:
            return False
        current_binding = _binding(dict(raw), Path(home) / 'state.db')
    except ValueError:
        return False
    from hermes_state_background import _target
    if value.get('target') != _target(conn, binding['session_id']):
        return False
    if current_binding != binding or retired_in(conn, binding['session_id']):
        return False
    session = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (binding['session_id'],)).fetchone()
    return session is not None and session[0] == binding['generation']


def _read(conn, admission_id):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX+admission_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(conn, admission_id, value):
    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(value), PREFIX+admission_id))


def claim(db, admission_id, kind, content, *, recovering=False, expected_snapshot=None):
    """One real SQLite writer wins; live owner and drift checks precede the CAS."""
    from gateway.delivery_ledger import _owner_alive as _pid_alive, _start_time
    home = str(Path(db.db_path).resolve().parent)
    def write(conn):
        value = _read(conn, admission_id)
        if value is None:
            return None, False, 'missing'
        from gateway.pending_final_claim import matches, snapshot
        if not matches(value, admission_id, expected_snapshot):
            return value, False, 'snapshot_changed'
        if value.get('expired') or time.time() - value['created_at'] > RETENTION_SECONDS:
            return value, False, 'expired'
        part = value['parts'].get(kind)
        if part is None or part['content'] != content or part['payload_hash'] != _digest(content):
            return value, False, 'payload_mismatch'
        if not _valid(conn, value, home):
            return value, False, 'invalidated'
        epoch = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()[0]
        if not recovering and epoch != value['binding']['owner_epoch']:
            return value, False, 'stale_epoch'
        live = _pid_alive(value['owner_pid'], value['owner_started_at'])
        own = value['owner_pid'] == os.getpid() and value['owner_started_at'] == _start_time(os.getpid())
        if live and (recovering or not own):
            return value, False, 'live_owner'
        if part['state'] != 'pending':
            if part['state'] == 'attempting' and not _pid_alive(part['owner_pid'], part['owner_started_at']):
                part.update(state='unknown', error='owner exited without durable ACK')
                _put(conn, admission_id, value)
            return value, False, part['state']
        text = value['parts'].get('text')
        from gateway.session_delivery_policy import STRICT
        if kind == 'attachments' and value['binding']['policy'] == STRICT and text and text['state'] != 'delivered':
            return value, False, 'earlier_part_' + text['state']
        part.update(state='attempting', attempts=1, owner_pid=os.getpid(),
                    owner_started_at=_start_time(os.getpid()), delivery_epoch=epoch, claim_snapshot=snapshot(value))
        _put(conn, admission_id, value)
        return value, True, 'attempting'
    return db._execute_write(write)


def finish(db, admission_id, kind, result, *, expected_snapshot=None):
    from gateway.delivery_ledger import _start_time
    def write(conn):
        value = _read(conn, admission_id)
        from gateway.pending_final_claim import matches
        if value is None or not matches(value, admission_id):
            return False
        part = value['parts'][kind]
        if part['state'] != 'attempting' or (part['owner_pid'], part['owner_started_at']) != (os.getpid(), _start_time(os.getpid())):
            raise RuntimeError('pending final send owner changed')
        epoch = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()[0]
        if part['delivery_epoch'] != epoch:
            raise RuntimeError('pending final send epoch changed')
        from gateway.pending_final_claim import valid
        if not valid(conn, db, value, kind, expected=expected_snapshot):
            part.update(state='unknown', error='pending final claim invalidated before ACK commit')
            _put(conn, admission_id, value)
            return False
        publication = None
        if getattr(result, 'success', False):
            from gateway.pending_final_publication import from_result
            try:
                publication = from_result(conn, value, kind, result)
            except ValueError:
                part.update(state='unknown', error='pending final publication ACK invalid')
                _put(conn, admission_id, value)
                return False
        part.update(state='delivered' if getattr(result, 'success', False) else 'unknown',
                    error=str(getattr(result, 'error', '') or ''))
        if part['state'] == 'delivered':
            part['delivered_at'] = time.time()
            if publication is not None:
                part['publication_receipt'] = publication
            _check_part(kind, part, expired=False)
        _put(conn, admission_id, value)
        return True
    return db._execute_write(write)


def db_for_event(event):
    from gateway.session_delivery_policy import native_claim_for_event
    claim = native_claim_for_event(event)
    return getattr(claim, 'db', None), getattr(claim, 'admission', None)


def has_pending_final(event):
    db, admission_id = db_for_event(event)
    if db is None:
        return False
    with db._read_ctx() as conn:
        return _read(conn, admission_id) is not None


async def prepare_delivery(adapter, value, source):
    """Prepare the generic recovery calls, including explicit per-call claim identity.

    ``pending_final_transport.prepare`` derives a stable execution/claim pair from
    the sealed descriptor before calling claim-bound adapters, so this path does not
    mint an unbound plan or bypass the adapter's claim contract.
    """
    from gateway.pending_final_transport import prepare
    return await prepare(adapter, value, source)


async def prepare_event(adapter, event):
    db, admission_id = db_for_event(event)
    if db is None:
        return
    def read():
        with db._read_ctx() as conn:
            return _read(conn, admission_id)
    value = await asyncio.to_thread(read)
    if value is not None:
        from gateway.pending_final_transport import event_plans
        return value, await event_plans(adapter, event, value)


def move_event_state(source, target, marker_fields):
    from gateway.session_delivery_policy import claim_for_event, native_claim_for_event
    if (runtime := claim_for_event(source)) is not None:
        target._automation_final_claim = runtime
    if (runtime := native_claim_for_event(source)) is not None:
        target._native_final_claim = runtime
    for name in marker_fields:
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))
            delattr(source, name)


async def claim_event(event, kind, content, *, route=None, adapter_profile=None, check_profile=False,
                      metadata=None, reply_to=None, check_context=False):
    from gateway.session_delivery_policy import native_claim_for_event
    runtime = native_claim_for_event(event)
    if runtime is not None and json.dumps(event.source.to_dict(), sort_keys=True) != runtime.destination:
        raise RuntimeError('pending final destination changed')
    db, admission_id = db_for_event(event)
    if db is None:
        return None
    def read():
        with db._read_ctx() as conn:
            return _read(conn, admission_id)
    value = await asyncio.to_thread(read)
    if value is None:
        return None
    if route is not None and value['binding']['route'] != route:
        raise RuntimeError('pending final route changed')
    if check_profile and value['adapter_profile'] != adapter_profile:
        raise RuntimeError('pending final adapter profile changed')
    if check_context and (value['metadata'] != (metadata or {}) or
            kind == 'text' and value['reply_to'] != reply_to):
        raise RuntimeError('pending final delivery context changed')
    from gateway.pending_final_claim import snapshot
    expected = snapshot(value)
    cache = getattr(event, '_prepared_final_transport', None)
    if cache is not None and cache[1] != expected:
        raise RuntimeError('pending final prepared context changed')
    value, won, state = await asyncio.to_thread(claim, db, admission_id, kind, content, expected_snapshot=expected)
    return db, admission_id, won, state, value


async def finish_event(receipt, kind, result):
    if receipt is None:
        return result
    try:
        from gateway.pending_final_claim import snapshot
        if await asyncio.to_thread(finish, receipt[0], receipt[1], kind, result,
                                   expected_snapshot=snapshot(receipt[4])) is False:
            raise RuntimeError('pending final claim invalidated')
    except Exception:
        logger.exception('Final ACK was not durably recorded')
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error='final_delivery_unknown: ACK persistence failed')
    return result


async def recover(runner):
    """Fresh startup consumes sealed pending payloads; it never enters a model runner."""
    from gateway.session import SessionSource
    from gateway.platforms.base import SendResult
    from gateway.strict_transport import strict_call
    authorities = list(getattr(runner, 'session_authorities', ()) or ())
    if not authorities and getattr(runner, 'session_authority', None) is not None:
        authorities = [runner.session_authority]
    delivered = 0
    for authority in authorities:
        await asyncio.to_thread(reconcile_orphans, authority.db)
        for value in await asyncio.to_thread(pending, authority.db):
            if value.get('expired'):
                continue
            from gateway.session_authorities import owner_scope
            from gateway.session_envelope import check_native_route
            from hermes_state_runtime import get_session_admission
            try:
                source = SessionSource.from_dict(value['binding']['source'])
                adapter = runner._delivery_adapter_for(source)
                if adapter is None or getattr(adapter, '_owner_profile', None) != value['adapter_profile']:
                    continue
                entry = runner.session_store.lookup_by_session_key(value['binding']['route'])
                if entry is None or entry.session_id != value['binding']['session_id'] or entry.suspended:
                    continue
                row = await asyncio.to_thread(get_session_admission, authority.db,
                                               admission_id=value['binding']['admission_id'])
                payload = deepcopy(row['payload'])
                payload['native_text_v1'].pop('media', None)  # input bytes were released by settlement
                with owner_scope(authority):
                    checked, _route = await check_native_route(runner, payload, entry.session_id, source, adapter)
                if checked.to_dict() != source.to_dict():
                    continue
            except Exception:
                logger.warning('Sealed final remains pending: current route authorization refused', exc_info=True)
                continue
            try:
                plans = await prepare_delivery(adapter, value, source)
            except Exception:
                logger.warning('Sealed final remains pending: transport capability or payload unavailable', exc_info=True)
                continue
            for kind, part, calls in plans:
                from gateway.pending_final_claim import current, snapshot
                expected = snapshot(value)
                claimed, won, state = await asyncio.to_thread(claim, authority.db, value['binding']['admission_id'],
                    kind, part['content'], recovering=True, expected_snapshot=expected)
                if not won:
                    continue
                receipt = authority.db, value['binding']['admission_id'], won, state, claimed
                results = []
                try:
                    for method, kwargs in calls:
                        if not await asyncio.to_thread(current, receipt, kind):
                            raise DeliveryUncertain('pending final claim invalidated')
                        if value['binding']['policy'] == 'human_reply_v1':
                            operation = adapter._send_with_retry if method == 'send' else getattr(adapter, method)
                            result = await operation(**kwargs)
                        else:
                            result = await strict_call(adapter, method, **kwargs)
                        results.append(result)
                        if not getattr(result, 'success', False):
                            break
                    result = results[0] if kind == 'text' and len(results) == 1 else SendResult(
                                        success=bool(results) and all(getattr(r, 'success', False) for r in results),
                                        error=None if results and all(getattr(r, 'success', False) for r in results) else 'ACK missing')
                except Exception as exc:
                    logger.exception('Sealed final transport raised after claim; delivery is unknown')
                    result = SendResult(success=False, error=str(exc))
                if (kind == 'text' and value['binding']['policy'] == 'human_reply_v1'
                        and results and not result.success):
                    from gateway.delivery_ledger import record_human_final_failure
                    await asyncio.to_thread(record_human_final_failure, claimed, str(result.error or ''), db=authority.db)
                result = await finish_event(receipt, kind, result)
                delivered += int(result.success)
                if not result.success:
                    break
    return delivered
