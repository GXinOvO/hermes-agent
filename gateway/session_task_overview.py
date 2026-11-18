import asyncio
import json
import logging
from pathlib import Path
import time

from gateway.session_busy_controls import authorize
from hermes_state_runtime import (_conversation_revision, _holder_of, _latest_todo, _session_row,
                                  RuntimeStoreError)

MAX_MESSAGES = 20000
logger = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024 * 1024
_TOOL_NAMES = frozenset({'todo', 'todo_list', 'patch', 'write_file', 'terminal', 'execute_code',
                         'browser_exec', 'delegate_task', 'tool_call'})


def _source_binding(authority, ref):
    live = authority.sessions.get(ref.session_id)
    if live is None:
        raise RuntimeStoreError('session_source_changed')
    return live.route, live.source.to_dict() if live.source is not None else None


def _target(authority, ref, stored_id):
    db = authority.persistence
    from persistence.sqlite.authority._local import local_physical_target
    with _holder_of(db)._read_ctx() as conn:
        target = local_physical_target(conn, ref.session_id)
    if target == ref.session_id:
        target = db.lineage.tip(ref.session_id) or ref.session_id
    row = _session_row(db, target)
    if row is None or _session_row(db, stored_id) is None:
        raise RuntimeStoreError('not_found')
    live = authority.sessions[ref.session_id]
    from gateway.config import Platform
    if live.source is not None and live.source.platform == Platform.LOCAL:
        from gateway.session_local_recovery import owned_receipt
        _, source, route, entry = owned_receipt(authority, ref.session_id)
        if (source.to_dict() != live.source.to_dict() or route != live.route or entry.session_id != target):
            raise RuntimeStoreError('session_source_changed')
    if row.get('session_key') and row['session_key'] != live.route:
        raise RuntimeStoreError('session_source_changed')
    lineage = db.lineage.resume_lineage_ids(target)
    if ((stored_id not in lineage and stored_id != ref.session_id)
            or (db.lineage.tip(stored_id) or stored_id) != target):
        raise RuntimeStoreError('session_source_changed', field='stored_id')
    return target


def _workspace(authority, ref, target):
    from gateway.session_policy import policy_for_source
    policy = policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
    cwd = policy.cwd if policy is not None else (authority._session_row(db, target) or {}).get('cwd')
    return cwd if isinstance(cwd, str) and cwd and Path(cwd).is_absolute() else None


def _verification(authority, target, cwd):
    result = {'verification': None, 'verification_cwd': cwd, 'verification_error': 'Session workspace unavailable'}
    if cwd is None or not Path(cwd).is_dir():
        return result
    from agent.verification_evidence import verification_status
    from agent.runtime_scope import bind_profile_scope
    home = Path(authority.profile_id)
    if not home.is_absolute():
        result['verification_error'] = 'Session verification unavailable'
        return result
    try:
        with bind_profile_scope(home, hydrate_secrets=False):
            result['verification'] = verification_status(session_id=target, cwd=cwd, read_only=True)
        result['verification_error'] = None
    except Exception:
        logger.debug('Session verification unavailable', exc_info=True)
        result['verification_error'] = 'Session verification unavailable'
    return result


def _messages(db, target):
    ids = db.lineage.resume_lineage_ids(target)
    rows = db._dedupe_display_generations(db._fetch_conversation_rows(
        ids, db._active_clause(False, True), with_session_id=False, with_compacted=True), drop_removed=True)
    if len(rows) > MAX_MESSAGES:
        raise RuntimeStoreError('overview_too_large', detail='Session exceeds the 20000 row limit')
    messages = []
    for raw in rows:
        row = db._row_to_message_dict(raw, warn_context='session.task_overview', summary_flag=True)
        role = row.get('role')
        if role not in ('user', 'assistant', 'tool'):
            continue
        message = {key: row[key] for key in ('role', 'timestamp', 'display_kind', '_compressed_summary')
                   if row.get(key) is not None}
        message['row_id'] = row['id']
        if role == 'tool':
            name = row.get('tool_name') or row.get('name') or ''
            if name not in _TOOL_NAMES:
                continue
            message['name'] = name
        else:
            content = row.get('content')
            if isinstance(content, list):
                content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict)
                                    and part.get('type') in ('text', 'input_text', 'output_text')
                                    and isinstance(part.get('text', ''), str))
            message['content'] = content if isinstance(content, str) else ''
            if role == 'assistant' and row.get('tool_calls'):
                message['tool_calls'] = [{'type': 'omitted'}]
        messages.append(message)
    return messages


def _read(authority, ref, stored_id, guard):
    from tui_gateway.tool_progress import _normalize_todo_state
    db = authority.persistence
    with _holder_of(db).transcript_read_snapshot() as consistent:
        guard(db.state_meta.get('store_instance_id'))
        target = _target(authority, ref, stored_id)
        revision = _conversation_revision(db, target)
        cwd = _workspace(authority, ref, target)
        messages = _messages(db, target)
        todo = _latest_todo(db, target)
        todo_state = _normalize_todo_state(json.loads(todo)) if todo is not None else None
        verification = _verification(authority, target, cwd)
        if not consistent():
            raise RuntimeStoreError('revision_conflict')
    guard(db.state_meta.get('store_instance_id'))
    if (_target(authority, ref, stored_id) != target
            or _conversation_revision(db, target) != revision
            or _workspace(authority, ref, target) != cwd):
        raise RuntimeStoreError('revision_conflict')
    return {'resolved_id': target, 'conversation_revision': json.loads(json.dumps(revision)),
            'messages': messages, 'todo_state': todo_state, **verification}


async def task_overview(connection, ref, params):
    for field in ('session_id', 'stored_id'):
        value = params.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise RuntimeStoreError('invalid_params', field=field)
    if params.get('expected_owner') is None:
        raise RuntimeStoreError('invalid_params', field='expected_owner')
    _, guard = await connection._expected_owner_guard(ref, params)
    authority, db = connection.authority, connection.authority.persistence
    from gateway.session_local_adoption import _due
    guard(await connection._read_store_instance_id())
    with _holder_of(db).transcript_read_snapshot() as consistent:
        if not consistent.pinned:
            raise RuntimeStoreError('storage_unavailable', detail='A stable read snapshot is unavailable')
        if _due(authority, ref.session_id) is not None:
            raise RuntimeStoreError('storage_unavailable', detail='Session compression recovery is pending')
        authorize(connection, ref, params, 'session:read', allow_local_adoption=False)
        binding = _source_binding(authority, ref)
        if not consistent():
            raise RuntimeStoreError('revision_conflict')
    data = await asyncio.to_thread(_read, authority, ref, params['stored_id'], guard)
    guard(await connection._read_store_instance_id())
    if _holder_of(db)._db_file_was_replaced():
        raise RuntimeStoreError('session_owner_changed')
    if (_source_binding(authority, ref) != binding
            or _target(authority, ref, params['stored_id']) != data['resolved_id']
            or _conversation_revision(db, data['resolved_id']) !=
            data['conversation_revision']):
        raise RuntimeStoreError('revision_conflict')
    authorize(connection, ref, params, 'session:read', allow_local_adoption=False)
    guard(db.state_meta.get('store_instance_id'))
    result = {'version': 1, 'session_id': ref.session_id, 'stored_session_id': params['stored_id'],
              'owner': dict(params['expected_owner']), 'authority_epoch': authority.epoch,
              'snapshot_scope': 'persisted_display', 'fetched_at': int(time.time() * 1000),
              'complete': True, **data}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        raise RuntimeStoreError('overview_too_large', detail='Session exceeds the 2MiB response limit')
    return result
