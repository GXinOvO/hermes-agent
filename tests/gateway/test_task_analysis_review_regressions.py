import asyncio
import json
import sqlite3
import sys
import threading
import time

import pytest

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
async def test_bare_session_reference_is_recalled_from_current_profile(linked, monkeypatch):
    from agent import oneshot

    db, sid = linked.db, linked.ref.session_id
    db.create_session('bare-history', source='cli')
    db.append_message('bare-history', 'user', 'Acceptance still requires a real client')
    db.append_message(sid, 'user', 'Consult @session:bare-history for the remaining acceptance')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    source = next((source for source in reply['result']['analysis']['sources']
                   if source['stored_session_id'] == 'bare-history'), None)
    assert source is not None, reply['result']['analysis']['sources']
    assert source['profile'] == 'default' and source['relation'] == 'reference' and source['status'] == 'read'


@pytest.mark.asyncio
async def test_scroll_string_anchor_rebounds_to_the_returned_session(linked, monkeypatch):
    from agent import oneshot
    from tools.session_search_tool import session_search

    db, sid = linked.db, linked.ref.session_id
    db.create_session('scroll-parent-string', source='cli')
    db.append_message('scroll-parent-string', 'user', 'Parent context')
    db.create_session('scroll-child-string', source='cli', parent_session_id='scroll-parent-string',
                      model_config={'_branched_from': 'scroll-parent-string'})
    anchor = db.append_message('scroll-child-string', 'user', 'Actual branched source requirement')
    args = {'session_id': 'scroll-parent-string', 'around_message_id': str(anchor)}
    actual = session_search(db=db, current_session_id=sid, **args)
    scroll = json.loads(actual)
    assert scroll['success'] is True and scroll['mode'] == 'scroll' and scroll['session_id'] == 'scroll-child-string'
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'scroll-string', 'type': 'function',
                      'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
    db.append_message(sid, 'tool', actual, tool_name='session_search', tool_call_id='scroll-string')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    source = next((source for source in reply['result']['analysis']['sources']
                   if source['stored_session_id'] == 'scroll-child-string'), None)
    assert source is not None and source['status'] == 'read', reply['result']['analysis']['sources']


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', ['@session:other/profile-precedence', 'other/profile-precedence'])
async def test_explicit_profile_overrides_profile_in_session_link(linked, monkeypatch, reference):
    from agent import oneshot
    from tools import session_search_tool

    db, sid = linked.db, linked.ref.session_id
    db.create_session('profile-precedence', source='cli')
    db.append_message('profile-precedence', 'user', 'Current profile contains this exact task')
    monkeypatch.setattr(session_search_tool, '_resolve_profile_db', lambda profile: None)
    args = {'session_id': reference, 'profile': 'default'}
    native = session_search_tool.session_search(db=db, current_session_id=sid, **args)
    payload = json.loads(native)
    assert payload['success'] is True and payload['session_id'] == 'profile-precedence'
    assert payload['link'] == '@session:default/profile-precedence'
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'precedence', 'type': 'function',
                      'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
    db.append_message(sid, 'tool', native, tool_name='session_search', tool_call_id='precedence')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    source = next((source for source in reply['result']['analysis']['sources']
                   if source['stored_session_id'] == 'profile-precedence'), None)
    assert source and source['profile'] == 'default' and source['status'] == 'read', source


@pytest.mark.asyncio
async def test_legacy_wrapped_read_requires_source_authorization(linked, monkeypatch):
    from agent import oneshot
    from tools.session_search_tool import _read_session
    from tools.tool_search_validation import normalize_tool_call_entries

    db, sid = linked.db, linked.ref.session_id
    db.create_session('private-legacy', source='cli')
    db.append_message('private-legacy', 'user', 'Confidential copied transcript')
    args = {'name': 'session_search', 'arguments': {'session_id': 'private-legacy', 'profile': 'other'}}
    assert normalize_tool_call_entries(args) == ([{'name': 'session_search', 'arguments': args['arguments']}], None)
    actual = _read_session(db, 'private-legacy', link_profile='other')
    assert json.loads(actual)['messages'][0]['content'] == 'Confidential copied transcript'
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'legacy-read', 'type': 'function',
                      'function': {'name': 'tool_call', 'arguments': json.dumps(args)}}])
    db.append_message(sid, 'tool', actual, tool_name='tool_call', tool_call_id='legacy-read')
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    source = next((source for source in reply['result']['analysis']['sources']
                   if source['stored_session_id'] == 'private-legacy'), None)
    assert source and source['profile'] == 'other' and source['status'] == 'unavailable'
    assert reply['result']['analysis']['complete'] is False
    assert all('Confidential copied transcript' not in row['content'] for row in captured[0]['messages'])


@pytest.mark.asyncio
async def test_missing_delegated_child_is_reported_incomplete(linked, monkeypatch):
    from agent import oneshot

    db, sid = linked.db, linked.ref.session_id
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'missing-delegate', 'type': 'function',
                      'function': {'name': 'delegate_task', 'arguments': '{}'}}])
    db.append_message(sid, 'tool', json.dumps({'status': 'dispatched', 'subagent_ids': ['sa-missing']}),
                      tool_name='delegate_task', tool_call_id='missing-delegate')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    analysis = reply['result']['analysis']
    assert analysis['complete'] is False
    assert any('sa-missing' in message for message in analysis['limitations']), analysis['limitations']


@pytest.mark.asyncio
async def test_primary_writer_contention_does_not_stall_gateway_loop(linked, monkeypatch):
    from agent import oneshot
    from tests.gateway.test_session_task_analysis import rpc

    entered, release, before_finalize = threading.Event(), threading.Event(), threading.Event()

    def model(**kwargs):
        entered.set()
        assert release.wait(8)
        return json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input'])))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    from gateway import session_task_sources
    real_fence = session_task_sources.commit_fence

    def observed_fence(graph, primary):
        before_finalize.set()
        return real_fence(graph, primary)

    monkeypatch.setattr(session_task_sources, 'commit_fence', observed_fence)
    params = {**linked.params, 'task_id': 'contention', 'title': 'Release', 'refresh': True, 'follow_sources': True}
    timeout_before = linked.db._conn.execute('PRAGMA busy_timeout').fetchone()[0]
    assert await rpc(linked, 'session.task_analysis', params) is None
    assert await asyncio.to_thread(entered.wait, 5)
    holder_script = '''import sqlite3,sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
print("ready", flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
'''
    holder = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', holder_script, str(linked.db.db_path),
                                                   stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    assert holder.stdin and holder.stdout
    try:
        assert (await asyncio.wait_for(holder.stdout.readline(), 5)).strip() == b'ready'
        start = time.monotonic()
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(before_finalize.wait, 5), 6)
        delay = time.monotonic() - start
        assert delay < 0.5, f'Gateway loop blocked for {delay:.2f}s during primary writer contention'
        failed = await asyncio.wait_for(linked.connection.transport.replies.get(), 5)
        assert 'error' in failed, failed
        assert not linked.db.list_meta_prefix('gateway.task_analysis.v2:')
        assert linked.db._conn.execute('PRAGMA busy_timeout').fetchone()[0] == timeout_before
        assert not linked.db._conn.in_transaction
    finally:
        release.set()
        if holder.returncode is None:
            holder.stdin.write(b'\n')
            await holder.stdin.drain()
            await asyncio.wait_for(holder.wait(), 5)
        holder.stdin.close()
        await holder.stdin.wait_closed()
        await linked.connection.close()


def test_busy_timeout_restore_failure_rolls_back_new_transaction(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / 'state.db')
    real = db._conn
    prior = real.execute('PRAGMA busy_timeout').fetchone()[0]
    calls = []

    class RestoreFailure:
        def __init__(self, conn):
            self.conn = conn
            self.fail = True

        def execute(self, sql, *args, **kwargs):
            if self.fail and sql == f'PRAGMA busy_timeout={int(prior)}':
                self.fail = False
                raise sqlite3.OperationalError('injected restoration failure')
            return self.conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    db._conn = RestoreFailure(real)
    try:
        with pytest.raises(sqlite3.OperationalError, match='restoration failure'):
            db._execute_write(lambda conn: calls.append('executed'), patience_s=0)
        assert calls == []
        assert not real.in_transaction
        assert real.execute('PRAGMA busy_timeout').fetchone()[0] == prior
        db._conn = real
        db.create_session('after-restore-failure', source='cli')
    finally:
        if real.in_transaction:
            real.rollback()
        db._conn = real
        db.close()
