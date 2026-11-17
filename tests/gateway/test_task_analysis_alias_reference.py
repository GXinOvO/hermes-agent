import json

import pytest

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize(('profile', 'expected', 'status'), [
    ('default', 'default', 'read'),
    ('other', 'other', 'unavailable'),
    ('Default', 'default', 'read'),
    ('Other', 'other', 'unavailable'),
])
async def test_producer_session_link_in_user_message_is_recalled(linked, monkeypatch, profile, expected, status):
    from agent import oneshot
    from tools.session_search_tool import _session_link

    db, sid = linked.db, linked.ref.session_id
    db.create_session('alias-reference', source='cli')
    db.append_message('alias-reference', 'user', 'Original task evidence')
    link = _session_link('alias-reference', profile)
    assert link == f'@session:{profile}/alias-reference'
    db.append_message(sid, 'user', f'Consult {link} for the task')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    response = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in response, response
    analysis = response['result']['analysis']
    matching = [source for source in analysis['sources'] if source['stored_session_id'] == 'alias-reference']
    assert len(matching) == 1, analysis['sources']
    assert matching[0]['profile'] == expected and matching[0]['status'] == status
    if status == 'unavailable':
        assert matching[0]['reason'] == 'profile_mismatch' and analysis['complete'] is False
