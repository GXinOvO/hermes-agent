"""Read-only data command for the Task Overview desktop plugin.

Use command.dispatch with its owning runtime session; Hermes injects the
current durable session ID under the correct profile context. No tool, model,
execution hook, task DB or background polling is introduced here.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
import time

logger = logging.getLogger(__name__)

COMMAND = 'task-overview-read'
MAX_MESSAGES = 20000
MAX_BYTES = 2 * 1024 * 1024


def _verification(session_id: str, session_key: str) -> dict:
    from agent.runtime_cwd import scoped_session_cwd
    from gateway.session_context import bound_session_key
    cwd = scoped_session_cwd()
    result = {'verification': None, 'verification_cwd': None, 'verification_error': 'Session workspace unavailable'}
    if bound_session_key() != session_key or not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        return result
    result['verification_cwd'] = cwd
    try:
        from agent.verification_evidence import verification_status
        result['verification'] = verification_status(session_id=session_id, cwd=cwd)
        result['verification_error'] = None
    except Exception:
        logger.exception('Task overview verification read failed')
        result['verification_error'] = 'Session verification unavailable'
    return result


def _read(session_id: str, requested_id: str, session_key: str, *, stored_read: bool = False) -> dict:
    from hermes_state import SessionDB
    db = SessionDB(read_only=True)
    try:
        requested = db.get_session(requested_id)
        if not requested:
            raise ValueError('Requested session not found')
        # The stored-read path remains within the calling session's profile.
        # Its target is resolved without resuming or attaching that target.
        resolved = db.get_compression_tip(requested_id) or requested_id
        if stored_read:
            session_id = resolved
        elif resolved != session_id:
            raise ValueError('Session binding changed; refresh the focused session')
        revision = db.conversation_revision(session_id, include_ancestors=True)
        rows = db.get_messages(session_id, include_compacted=True, include_ancestors=True, limit=MAX_MESSAGES + 1)
        if len(rows) > MAX_MESSAGES:
            raise ValueError('Session exceeds safe read limit; no partial task overview returned')
        # Native pairing excludes orphan/forged results and preserves clears,
        # removed copies and compression/branch semantics. Raw result shape
        # alone is not evidence that todo_list was called.
        todo_result = db.get_latest_todo_result(session_id)
        todo_state = json.loads(todo_result) if todo_result is not None else None
        # Only the focused path may bind workspace verification. A foreign stored
        # session cannot inherit the calling session's cwd or create a ledger.
        verification = ({'verification': None, 'verification_cwd': None,
                         'verification_error': 'Stored-session verification not queried'}
                        if stored_read else _verification(session_id, session_key))
        if db.conversation_revision(session_id, include_ancestors=True) != revision or (db.get_compression_tip(requested_id) or requested_id) != session_id:
            raise ValueError('Session changed while reading; refresh the focused session')
        # Keep tool occurrence metadata for conservative record expiry, but
        # never ship arbitrary tool results or model reasoning/arguments.
        messages = []
        for row in rows:
            role = row.get('role')
            if role not in ('user', 'assistant', 'tool'):
                continue
            msg = {k: row[k] for k in ('role', 'content', 'timestamp', 'display_kind', '_compressed_summary') if k in row}
            msg['row_id'] = row.get('id', row.get('_row_id'))
            if role == 'tool':
                name = row.get('tool_name') or row.get('name') or ''
                msg['name'] = name
                msg.pop('content', None)
                if name not in ('todo', 'todo_list', 'patch', 'write_file', 'terminal', 'execute_code', 'browser_exec', 'delegate_task', 'tool_call'):
                    continue
            if row.get('tool_calls'):
                msg['tool_calls'] = row['tool_calls']
                if role == 'assistant':
                    # Keep only the fact this row has tool calls; arguments may
                    # contain irrelevant sensitive inputs and are not evidence.
                    msg['tool_calls'] = [{'type': 'omitted'}]
            if isinstance(msg.get('content'), list):
                msg['content'] = '\n'.join(p.get('text', '') for p in msg['content'] if isinstance(p, dict) and p.get('type') in ('text', 'input_text', 'output_text'))
            messages.append(msg)
        payload = {'version': 2, 'ok': True, 'requested_id': requested_id, 'resolved_id': session_id,
                   'snapshot_scope': 'persisted_display', 'todo_state': todo_state,
                   'conversation_revision': revision,
                   'fetchedAt': int(time.time() * 1000), 'messages': messages,
                   'count': len(messages), 'raw_count': len(rows), 'complete': True, **verification}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BYTES:
            raise ValueError('Session exceeds safe response size; no partial task overview returned')
        return payload
    finally:
        db.close()


def read_command(raw_args: str, *, session_id: str = '', session_key: str = '', **context) -> str:
    try:
        args = json.loads(raw_args or '{}')
        if not isinstance(args, dict) or set(args) != {'stored_id'} or not isinstance(args['stored_id'], str) or not args['stored_id']:
            raise ValueError('Exactly one non-empty stored_id is required')
        if not session_id or not session_key:
            raise ValueError('A live owning session context is required')
        payload = _read(session_id, args['stored_id'], session_key)
    except Exception as exc:
        logger.exception('Task overview read failed')
        # Preserve failure as an explicit envelope because command.dispatch's
        # compatibility layer suppresses handler exceptions into fallthrough.
        payload = {'version': 2, 'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    return json.dumps(payload, ensure_ascii=False)


def read_stored_command(raw_args: str, *, session_id: str = '', session_key: str = '', **context) -> str:
    try:
        args = json.loads(raw_args or '{}')
        if not isinstance(args, dict) or set(args) != {'stored_id'} or not isinstance(args['stored_id'], str) or not args['stored_id']:
            raise ValueError('Exactly one non-empty stored_id is required')
        if not session_id or not session_key:
            raise ValueError('A live owning session context is required')
        payload = _read(session_id, args['stored_id'], session_key, stored_read=True)
    except Exception as exc:
        logger.exception('Stored task overview read failed')
        payload = {'version': 2, 'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    return json.dumps(payload, ensure_ascii=False)


def register(ctx):
    ctx.register_command(COMMAND, read_command, description='Read-only task overview JSON for the owning session', args_hint='<stored-id-json>')
