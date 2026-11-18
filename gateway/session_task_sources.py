import asyncio
from contextlib import ExitStack, contextmanager
from persistence.core.errors import AuthorityRefusal
from persistence_assembly import store_location
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from gateway.session_task_analysis_input import _text, stored_rows
from gateway.session_task_relations import candidates, copied_session_results
from hermes_state_runtime import _conversation_revision, _holder_of, RuntimeStoreError

MAX_SOURCES = 8
MAX_DEPTH = 4
MAX_SOURCE_CHARS = 24000
MAX_INPUT_CHARS = 120000


@dataclass
class SourceRead:
    connection: Any
    params: dict
    ref: object
    target: str
    binding: object
    guard: Callable
    revision: object
    rows: list

    def check(self):
        from gateway.session_task_analysis import _checked
        from gateway.session_authorities import owner_scope
        registry = getattr(self.connection.authority.runner, 'session_authorities', None)
        if registry is not None and registry.for_home(self.connection.authority.profile_id) is not self.connection.authority:
            raise RuntimeStoreError('session_owner_changed')
        with owner_scope(self.connection.authority):
            _checked(self.connection, self.params, self.guard, self.target, self.binding, self.revision)


def _snapshot(connection, params, prepared, guard):
    ref, target, binding = prepared
    db = connection.authority.persistence
    with _holder_of(db).transcript_read_snapshot() as consistent:
        guard(db.state_meta.get('store_instance_id'))
        raw = stored_rows(db, target)
        revision = _conversation_revision(db, target)
        from gateway.session_controls import _profile_label
        relations, missing, unresolved = candidates(db, target, raw, params.get('profile') or _profile_label(connection.authority.profile_id))
        if not consistent():
            raise RuntimeStoreError('revision_conflict')
    read = SourceRead(connection, params, ref, target, binding, guard, revision, raw)
    read.check()
    return read, relations, missing, unresolved


def _project(raw, source_id, budget, excluded=()):
    rows = []
    copied = copied_session_results(raw)
    excluded = set(excluded)
    for item in raw:
        if item['id'] in copied or item['id'] in excluded:
            continue
        content = _text(item['content'])
        if content.strip():
            rows.append({'source_id': source_id, 'row_id': item['id'], 'role': item['role'],
                         'content': content, 'summary': bool(item['_compressed_summary'])})
    users = [i for i, row in enumerate(rows) if row['role'] == 'user' and not row['summary']]
    priority = list(dict.fromkeys([*users[:1], *users[-1:], *range(len(rows) - 1, -1, -1)]))
    selected, used, complete = {}, 0, True
    for i in priority:
        row = rows[i]
        content = row['content']
        if len(content) > 12000:
            content = content[:6000] + '\n[...truncated...]\n' + content[-6000:]
            complete = False
        if used + len(content) > budget:
            complete = False
            continue
        selected[i] = {**row, 'content': content}
        used += len(content)
    return [selected[i] for i in sorted(selected)], len(rows), complete, used


async def collect(connection, params):
    from gateway.session_controls import _profile_label
    from gateway.session_task_analysis import _prepare
    profile = params.get('profile') or _profile_label(connection.authority.profile_id)
    pending: list[tuple] = [(connection, profile, params['stored_id'], 'primary', None, None, 0)]
    sources, reads, rows, seen, limitations = [], [], [], set(), []
    total, used = 0, 0
    while pending:
        parent, label, sid, relation, via_source, via_row, depth = pending.pop(0)
        requested = (label, sid)
        if requested in seen:
            continue
        if len(sources) >= MAX_SOURCES:
            limitations.append('Source limit reached; some related conversations were not read.')
            break
        seen.add(requested)
        source_id = f's{len(sources)}'
        source = {'id': source_id, 'profile': label, 'session_id': sid, 'stored_session_id': sid,
                  'resolved_id': None, 'owner': None, 'relation': relation, 'via_source_id': via_source,
                  'via_row_id': via_row, 'status': 'unavailable', 'complete': False, 'reason': None}
        try:
            current = parent
            if label != _profile_label(parent.authority.profile_id):
                current = parent._sibling_for(label) or parent
            request = {'session_id': sid, 'stored_id': sid}
            if current is not parent:
                request['profile'] = label
            if relation == 'primary':
                request['expected_owner'] = params['expected_owner']
            prepared, guard, owner = await _prepare(current, request, sid)
            logical, target, _ = prepared
            key = (current.authority.profile_id, target)
            if key in seen:
                continue
            seen.add(key)
            request.update(session_id=logical.session_id, expected_owner=owner)
            read, related, missing, unresolved = await asyncio.to_thread(_snapshot, current, request, prepared, guard)
            limitations.extend(f'{source_id}: delegated child {child} could not be read or verified.' for child in missing)
            limitations.extend(f'{source_id}: related conversation in tool result row {row} could not be verified.' for row in unresolved)
            selected, count, complete, consumed = _project(
                read.rows, source_id, min(MAX_SOURCE_CHARS, MAX_INPUT_CHARS - used), unresolved)
            source.update(session_id=logical.session_id, resolved_id=target, owner=owner,
                          status='read', complete=complete, reason=None if complete else 'message_coverage_limit')
            sources.append(source)
            reads.append(read)
            rows.extend(selected)
            total += count
            used += consumed
            if not complete:
                limitations.append(f'{source_id}: message coverage was truncated.')
            if depth < MAX_DEPTH:
                pending.extend((current, profile_name, stored, kind, source_id, row_id, depth + 1)
                               for profile_name, stored, kind, row_id in related)
            elif related:
                limitations.append(f'{source_id}: relationship depth limit reached.')
        except (RuntimeStoreError, AuthorityRefusal) as exc:



            if relation == 'primary':
                raise
            source['reason'] = exc.reason
            sources.append(source)
            limitations.append(f'{source_id}: related source unavailable ({exc.reason}).')
    for read in reads:
        read.check()
    complete = not limitations and all(source['complete'] for source in sources)
    if not complete:
        limitations.insert(0, 'Linked-conversation coverage is incomplete; do not infer unseen work.')
    return {'sources': sources, 'messages': rows, 'message_count': total, 'complete': complete,
            'limitations': limitations, 'reads': reads}


@contextmanager
def commit_fence(graph, primary):
    own = str(store_location(primary.scope))
    foreign = {str(store_location(read.connection.authority.persistence.scope)): read.connection.authority.persistence
               for read in graph['reads']
               if str(store_location(read.connection.authority.persistence.scope)) != own}
    from persistence.sqlite._state.errors import StateDbReplacedError
    with ExitStack() as stack:
        for path in sorted(foreign):
            holder = _holder_of(foreign[path])
            if not holder._lock.acquire(blocking=False):
                raise RuntimeStoreError('storage_unavailable')
            stack.callback(holder._lock.release)
            if holder._conn is None:
                raise RuntimeStoreError('session_owner_changed')
            try:
                holder._raise_if_db_corrupt(storage=True)
                holder._raise_if_db_replaced()
            except StateDbReplacedError as exc:
                raise RuntimeStoreError('session_owner_changed') from exc
            except sqlite3.DatabaseError as exc:
                raise RuntimeStoreError('storage_unavailable') from exc
            previous_timeout = holder._conn.execute('PRAGMA busy_timeout').fetchone()[0]
            try:
                holder._conn.execute('PRAGMA busy_timeout=0')
                holder._conn.execute('BEGIN IMMEDIATE')
                stack.callback(holder._conn.rollback)
            except sqlite3.OperationalError as exc:
                raise RuntimeStoreError('storage_unavailable') from exc
            finally:
                holder._conn.execute(f'PRAGMA busy_timeout={int(previous_timeout)}')
            pins = holder._snapshot_pins
            pins.conn = holder._conn
            stack.callback(setattr, pins, 'conn', None)
        yield


def check(graph):
    for read in graph['reads']:
        read.check()


def revisions(graph):
    return [{'id': source['id'], 'profile': source['profile'], 'stored_id': source['stored_session_id'],
             'resolved_id': source['resolved_id'], 'owner': source['owner'],
             'revision': next((read.revision for read in graph['reads']
                               if read.target == source['resolved_id'] and read.connection.authority.profile_id == source['owner']['profile_id']), None)
             if source['owner'] else None} for source in graph['sources']]
