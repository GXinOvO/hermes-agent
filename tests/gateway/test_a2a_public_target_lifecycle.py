import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import pending_final
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.gateway.test_a2a_durable_public import recover, seed


async def mutate(home, operation):
    with SessionDB(home / 'state.db') as db:
        value = pending_final.pending(db)[0]
        sid = value['binding']['session_id']
        route = value['binding']['route']
        epoch = begin_runtime_epoch(db, instance_id='target-mutation-owner')
        runner = SimpleNamespace(_draining=False, _evict_cached_agent=lambda key: None)
        authority = SessionAuthority(runner, profile_id='owned', instance_id='target-mutation-owner', db=db, epoch=epoch)
        authority.sessions[sid] = LiveSession(None, route)
        owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
        try:
            current = db.get_session(sid)
            assert current is not None
            payload = {}
            if operation == 'rewind':
                db.append_message(sid, role='user', content='authorized rewind boundary')
                payload['target_message_id'] = db.get_messages(sid)[0]['id']
                current = db.get_session(sid)
            response = await owner.dispatch({'id': 1, 'method': 'session.mutate', 'params': {
                'session_id': sid, 'request_id': 'target-' + operation,
                'expected_revision': current['runtime_revision'],
                'expected_generation': current['runtime_generation'],
                'operation': operation, 'payload': payload}})
            assert 'result' in response, response
            if operation == 'delete':
                assert db.get_session(sid) is None
            else:
                assert db.get_session(sid)['runtime_generation'] > current['runtime_generation']
            return response
        finally:
            await owner.close()


def following_turn(home):
    from gateway.session_results import finish_result
    with SessionDB(home / 'state.db') as db:
        value = pending_final.pending(db)[0]
        binding = value['binding']
        epoch = begin_runtime_epoch(db, instance_id='following-turn-owner')
        payload = {'text': 'legitimate subsequent turn', 'native_text_v1': {
            'route': binding['route'], 'source': binding['source']}}
        admit_session_input(db, epoch=epoch, principal_id='messaging:' + binding['route'],
            session_id=binding['session_id'], request_id='following-native-input', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id=binding['session_id'])
        assert row['generation'] > binding['generation']
        finish_result(db, epoch=epoch, row=row, response='following turn answer', outcome='completed',
            result={'result': {'final_response': 'following turn answer', 'messages': []}, 'usage': {}})


@pytest.mark.parametrize('operation', ['unchanged', 'next-turn', 'delete', 'rewind'])
def test_published_a2a_result_obeys_authenticated_target_retirement(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = seed(tmp_path)
    first = recover(tmp_path, 'recover')
    assert first['delivered'] == 1
    assert all('sealed native answer' in text for text in first['public'].values())
    from plugins.platforms.a2a.protocol import TaskStore
    warm = TaskStore(tmp_path)
    assert warm.get(identity['task'])['reply'] == 'sealed native answer'
    if operation == 'next-turn':
        following_turn(tmp_path)
    mutation = asyncio.run(mutate(tmp_path, operation)) if operation in {'delete', 'rewind'} else None
    visible_expected = operation in {'unchanged', 'next-turn'}
    warm_record = warm.get(identity['task'])
    assert (warm_record is not None) is visible_expected
    reopened = recover(tmp_path, 'recover')
    replay = recover(tmp_path, 'resend')
    visible = {method: 'sealed native answer' in text for method, text in reopened['public'].items()}
    visible['replay'] = 'sealed native answer' in replay['response']
    print('A2A_TARGET_LIFECYCLE ' + json.dumps({'identity': identity, 'operation': operation,
        'mutation': mutation, 'visible': visible, 'part': reopened['part'], 'replay': replay,
        'executions': (tmp_path / 'executions').read_text()}))
    assert (tmp_path / 'executions').read_text() == '1'
    assert reopened['delivered'] == 0
    assert reopened['part']['attempts'] == 1
    assert all(value is visible_expected for value in visible.values()), visible
