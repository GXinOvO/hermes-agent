import json
import hashlib
import time
import uuid

from gateway.session_busy_controls import authorize
from gateway.session_contract import SessionRef
from gateway.session_task_overview import _source_binding, _target
from hermes_state_runtime import RuntimeStoreError


def _owner(authority):
    return {'instance_id': authority.instance_id, 'profile_id': authority.profile_id,
            'authority_epoch': authority.epoch, 'store_instance_id': authority.db.get_meta('store_instance_id')}


def _resolve(connection, stored_id, params):
    from gateway.session_authorities import require_serving
    from gateway.session_listing import legacy_readable
    from gateway.session_local_adoption import _due
    authority, db = connection.authority, connection.authority.db
    require_serving(authority)
    if 'session:read' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    from hermes_state_runtime import _epoch
    with db._read_ctx() as conn:
        _epoch(conn, authority.epoch)
    if not isinstance(stored_id, str) or not stored_id or stored_id != stored_id.strip() or len(stored_id) > 512:
        raise RuntimeStoreError('invalid_params', field='session_id')
    if db.get_session(stored_id) is None:
        raise RuntimeStoreError('not_found')
    tip = db.get_compression_tip(stored_id) or stored_id
    from hermes_state_local import POLICY_PREFIX, local_lineage_owner
    with db._read_ctx() as conn:
        local = local_lineage_owner(conn, tip)
        bound = conn.execute('SELECT 1 FROM state_meta WHERE key=?', (POLICY_PREFIX + local,)).fetchone()
    sid = local if bound else (tip if legacy_readable(authority, connection.actor, tip) else authority.logical_owner(stored_id))
    ref = SessionRef(connection.actor.profile_id, sid)
    if _due(authority, sid) is not None:
        raise RuntimeStoreError('storage_unavailable', detail='Session compression recovery is pending')
    try:
        authorize(connection, ref, params, 'session:read', allow_local_adoption=False)
    except RuntimeStoreError as exc:
        if exc.reason != 'not_found' or not legacy_readable(authority, connection.actor, tip):
            raise
        return ref, tip, ('legacy', db.get_session(tip))
    target = _target(authority, ref, stored_id)
    return ref, target, _source_binding(authority, ref)


async def _prepare(connection, params, stored_id):
    authority, db = connection.authority, connection.authority.db
    expected = params.get('expected_owner')
    if expected is None:
        expected = _owner(authority)
    guard_params = {'session_id': params['session_id'], 'expected_owner': expected}
    _, guard = await connection._expected_owner_guard(SessionRef(connection.actor.profile_id, params['session_id']), guard_params)
    guard(await connection._read_store_instance_id())
    with db.transcript_read_snapshot() as consistent:
        if not consistent.pinned:
            raise RuntimeStoreError('storage_unavailable')
        resolved = _resolve(connection, stored_id, params)
        if not consistent():
            raise RuntimeStoreError('revision_conflict')
    guard(db.get_meta('store_instance_id'))
    if db._db_file_was_replaced():
        raise RuntimeStoreError('session_owner_changed')
    return resolved, guard, expected


async def task_source(connection, ref, params):
    (logical, target, binding), guard, owner = await _prepare(connection, params, ref.session_id)
    guard(connection.authority.db.get_meta('store_instance_id'))
    if _resolve(connection, ref.session_id, params) != (logical, target, binding):
        raise RuntimeStoreError('session_source_changed')
    return {'session_id': logical.session_id, 'stored_session_id': ref.session_id, 'resolved_id': target,
            'owner': owner, 'authority_epoch': connection.authority.epoch}


def _key(sid, task_id, version=1):
    return f'gateway.task_analysis.v{version}:' + hashlib.sha256(json.dumps([sid, task_id]).encode()).hexdigest()


def _validate(params):
    for field, limit in (('session_id', 512), ('stored_id', 512), ('task_id', 200), ('title', 500)):
        value = params.get(field)
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
            raise RuntimeStoreError('invalid_params', field=field)
    if (params.get('expected_owner') is None or type(params.get('refresh', False)) is not bool
            or type(params.get('follow_sources', False)) is not bool):
        raise RuntimeStoreError('invalid_params')


def _checked(connection, params, guard, target, binding, revision):
    db = connection.authority.db
    with db._read_ctx() as conn:
        guard(conn.execute("SELECT value FROM state_meta WHERE key='store_instance_id'").fetchone()[0])
    if db._db_file_was_replaced():
        raise RuntimeStoreError('session_owner_changed')
    logical, current, bound = _resolve(connection, params['stored_id'], params)
    if logical.session_id != params['session_id'] or current != target or bound != binding:
        raise RuntimeStoreError('session_source_changed')
    if json.loads(json.dumps(db.conversation_revision(target, include_ancestors=True))) != revision:
        raise RuntimeStoreError('revision_conflict')


def _refresh(connection, ref, params, target, binding, guard, result, previous, graph=None):
    from gateway.session_task_analysis_runtime import runtime_for
    from gateway.session_task_analysis_input import INSTRUCTIONS, messages, validate_output
    from gateway.session_task_analysis_reply import AnalysisReply
    authority, db = connection.authority, connection.authority.db
    with db.transcript_read_snapshot() as consistent:
        revision = json.loads(json.dumps(db.conversation_revision(target, include_ancestors=True)))
        rows, count, limitations = messages(db, target)
        if not consistent():
            raise RuntimeStoreError('revision_conflict')
    _checked(connection, params, guard, target, binding, revision)
    instructions, validator, version, analysis_revision = INSTRUCTIONS, validate_output, 1, revision
    if graph is not None:
        from gateway.session_task_linked_input import LINKED_INSTRUCTIONS, validate_linked_output
        from gateway.session_task_sources import check, revisions
        check(graph)
        rows, count, limitations = graph['messages'], graph['message_count'], graph['limitations']
        instructions, version, analysis_revision = LINKED_INSTRUCTIONS, 2, revisions(graph)
        validator = lambda text, _: validate_linked_output(text, graph)
    runtime = runtime_for(authority, ref, target)
    cache_id = authority.logical_owner(params['stored_id'])
    key = _key(cache_id, params['task_id'], version)
    generations = getattr(authority, '_task_analysis_generations', None)
    if generations is None:
        generations = authority._task_analysis_generations = {}
    generation = uuid.uuid4().hex
    generations[key] = generation

    def work():
        from agent.oneshot import run_oneshot
        from agent.auxiliary_client import scoped_runtime_main
        from agent.auxiliary_task_config import session_auxiliary_scope
        from gateway.session_authorities import owner_scope
        with owner_scope(authority):
            if graph is not None:
                check(graph)
            main = runtime()
            if (not isinstance(main, dict) or not main.get('model')
                    or main.get('provider') in (None, '', 'auto')):
                raise RuntimeStoreError('analysis_runtime_unavailable')
            route = {field: main[field] for field in ('provider', 'model', 'base_url', 'api_key', 'api_mode')
                     if isinstance(main.get(field), str) and main[field]}
            with scoped_runtime_main(main), session_auxiliary_scope({'session_task_analysis': route}):
                output = run_oneshot(instructions=instructions,
                                     user_input=json.dumps({'title': params['title'], 'messages': rows,
                                                            'limitations': limitations,
                                                            **({'sources': graph['sources']} if graph is not None else {})}, ensure_ascii=False),
                                     task='session_task_analysis', main_runtime=main,
                                     max_tokens=6000, temperature=0, timeout=120)
            value = validator(output, rows)
        return {'version': version, 'title': params['title'], 'analyzed_at': int(time.time() * 1000),
                'conversation_revision': analysis_revision, 'stale': False,
                'scope': 'linked_conversations' if graph is not None else 'single_conversation',
                **({'complete': graph['complete'], 'sources': graph['sources']} if graph is not None else {}),
                'message_count': count, 'analyzed_messages': len(rows), **value,
                'limitations': [*limitations, *value['limitations']]}

    def finalize(analysis):
        from gateway.session_authorities import owner_scope
        from hermes_state_stored_reads import _pins
        from hermes_state_runtime import _epoch
        with owner_scope(authority):
            def write(conn):
                guard(conn.execute("SELECT value FROM state_meta WHERE key='store_instance_id'").fetchone()[0])
                _epoch(conn, result['authority_epoch'])
                if generations.get(key) != generation:
                    raise RuntimeStoreError('analysis_conflict')
                saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
                if (saved[0] if saved else None) != previous:
                    raise RuntimeStoreError('analysis_conflict')
                pins = _pins()
                pins[id(db)] = conn
                try:
                    _checked(connection, params, guard, target, binding, revision)
                    if graph is not None:
                        check(graph)
                finally:
                    pins.pop(id(db), None)
                record = {'generation': generation,
                          'binding': [cache_id, params['stored_id'], params['task_id'], params['title']],
                          'analysis': analysis}
                if graph is not None:
                    coverage = list(graph['limitations'])
                    record['coverage_limitations'] = coverage
                    record['model_limitations'] = analysis['limitations'][len(coverage):]
                conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (key, json.dumps(record, ensure_ascii=False)))
            if graph is None:
                db._execute_write(write, patience_s=0)
            else:
                from gateway.session_task_sources import commit_fence
                with commit_fence(graph, db):
                    db._execute_write(write, patience_s=0)
            saved = json.loads(db.get_meta(key))
            if saved['generation'] != generation:
                raise RuntimeStoreError('analysis_conflict')
            return {**result, 'analysis': saved['analysis']}
    return AnalysisReply(work, finalize)


async def task_analysis(connection, ref, params):
    _validate(params)
    (logical, target, binding), guard, owner = await _prepare(connection, params, params['stored_id'])
    if logical.session_id != ref.session_id:
        raise RuntimeStoreError('session_source_changed')
    db = connection.authority.db
    graph = None
    if params.get('follow_sources', False):
        from gateway.session_task_sources import collect
        graph = await collect(connection, params)
    cache_id = connection.authority.logical_owner(params['stored_id'])
    raw = db.get_meta(_key(cache_id, params['task_id'], 2 if graph is not None else 1))
    record = json.loads(raw) if raw else None
    analysis = record['analysis'] if record and record['binding'] == [cache_id, params['stored_id'], params['task_id'], params['title']] else None
    revision = json.loads(json.dumps(db.conversation_revision(target, include_ancestors=True)))
    if record is not None and analysis is not None:
        if graph is not None:
            from gateway.session_task_sources import revisions
            coverage_limitations = graph['limitations']
            cached_coverage = record.get('coverage_limitations')
            model_limitations = ([f'Previous analysis (unverified): {item}' for item in analysis['limitations']]
                                 if cached_coverage is None else record['model_limitations'])
            coverage_changed = analysis['complete'] != graph['complete'] or cached_coverage != coverage_limitations
            analysis['stale'] = (
                analysis['conversation_revision'] != revisions(graph)
                or analysis['sources'] != graph['sources']
                or coverage_changed
            )
            analysis['complete'] = analysis['complete'] and graph['complete'] and not coverage_changed
            analysis['limitations'] = list(dict.fromkeys([*coverage_limitations, *model_limitations]))
        else:
            analysis['stale'] = analysis['conversation_revision'] != revision
    result = {'session_id': logical.session_id, 'stored_session_id': params['stored_id'], 'owner': owner,
              'authority_epoch': connection.authority.epoch, 'task_id': params['task_id'], 'analysis': analysis}
    guard(db.get_meta('store_instance_id'))
    if params.get('refresh', False):
        return _refresh(connection, logical, params, target, binding, guard, result, raw, graph)
    _checked(connection, params, guard, target, binding, revision)
    if graph is not None:
        from gateway.session_task_sources import check
        check(graph)
        if analysis is None:
            old = db.get_meta(_key(cache_id, params['task_id']))
            legacy = json.loads(old) if old else None
            if legacy and legacy['binding'] == [cache_id, params['stored_id'], params['task_id'], params['title']]:
                result['analysis'] = legacy['analysis']
                result['analysis']['stale'] = True
    return result
