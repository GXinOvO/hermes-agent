import json

import pytest

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize(('before', 'after', 'legacy'), [
    ([], ['new gap'], False), (['old gap'], [], False),
    (['old gap'], ['new gap'], False), (['old gap'], [], True),
    (['kept gap', 'old gap'], ['kept gap'], False),
    (['kept gap', 'old gap'], ['kept gap', 'new gap'], False),
])
async def test_cached_coverage_changes_preserve_model_limits(linked, monkeypatch, before, after, legacy):
    from agent import oneshot
    from gateway import session_task_sources
    from gateway.session_task_analysis import _key

    params = {**linked.params, 'task_id': 'linked', 'title': 'Release', 'follow_sources': True}
    current = dict(await session_task_sources.collect(linked.connection, params))
    current.update(complete=not before, limitations=before)

    async def collect(*args, **kwargs):
        return current

    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        answer = fixtures.linked_answer(json.loads(kwargs['user_input']))
        answer['limitations'] = ['Model uncertainty']
        return json.dumps(answer)

    monkeypatch.setattr(session_task_sources, 'collect', collect)
    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    first = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    key = _key(linked.authority.logical_owner(linked.params['stored_id']), 'linked', 2)
    if legacy:
        record = json.loads(linked.db.get_meta(key))
        record.pop('coverage_limitations', None)
        record.pop('model_limitations', None)
        linked.db.set_meta(key, json.dumps(record))
    saved = linked.db.get_meta(key)
    changes = linked.db._conn.total_changes
    current.update(complete=not after, limitations=after)
    analysis = (await fixtures.run_linked(linked))['result']['analysis']
    assert analysis['stale'] is True
    assert analysis['complete'] is False
    assert analysis['goal'] == first['goal']
    if legacy:
        assert analysis['limitations'] == after + [f'Previous analysis (unverified): {item}' for item in first['limitations']]
    else:
        assert analysis['limitations'] == after + ['Model uncertainty']
    assert linked.db.get_meta(key) == saved and linked.db._conn.total_changes == changes
    assert len(calls) == 1
    assert (await fixtures.run_linked(linked))['result']['analysis'] == analysis
    fresh = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    assert fresh['stale'] is False and fresh['complete'] is (not after)
    assert fresh['limitations'] == after + ['Model uncertainty']


@pytest.mark.asyncio
async def test_unavailable_source_recovery_requires_refresh(linked, monkeypatch):
    from agent import oneshot

    linked.db.append_message(linked.ref.session_id, 'user', 'Consult @session:default/recovered-source')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(
        fixtures.linked_answer(json.loads(kwargs['user_input']))))
    first = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    assert first['complete'] is False and first['stale'] is False
    assert any(source['stored_session_id'] == 'recovered-source' and source['status'] == 'unavailable'
               for source in first['sources'])
    linked.db.create_session('recovered-source', source='cli')
    linked.db.append_message('recovered-source', 'user', 'Recovered source is now readable')
    cached = (await fixtures.run_linked(linked))['result']['analysis']
    assert cached['complete'] is False and cached['stale'] is True
    assert cached['goal'] == first['goal'] and cached['limitations'] == []
    fresh = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    assert fresh['complete'] is True and fresh['stale'] is False
    assert any(source['stored_session_id'] == 'recovered-source' and source['status'] == 'read'
               for source in fresh['sources'])
