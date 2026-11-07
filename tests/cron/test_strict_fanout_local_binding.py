import pytest

from tests.cron.test_strict_mixed_fanout import enqueue, owner, peer


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['claimed', 'commit'])
@pytest.mark.parametrize('damage', ['source', 'route', 'profile', 'epoch'])
async def test_local_plan_drift_refused_before_admission(tmp_path, monkeypatch, when, damage):
    enqueue(tmp_path, monkeypatch, deliver='origin,local:LOCAL_TARGET,slack:C12345678:1700000777.000001',
            local=True, drift_at=when, drift=damage)
    async with peer() as (url, events):
        first = await owner(tmp_path, url)
        wire = list(events)
        second = await owner(tmp_path, url)
    assert first['row']['status'] == second['row']['status'] == 'unknown'
    assert first['local_admissions'] == second['local_admissions'] == []
    assert first['local_messages'] == second['local_messages'] == []
    assert events == wire and len(events) == 2


@pytest.mark.asyncio
async def test_unchanged_local_frozen_plan_admitted_once(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch, deliver='origin,local:LOCAL_TARGET,slack:C12345678:1700000777.000001', local=True)
    async with peer() as (url, events):
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    assert first['row']['status'] == second['row']['status'] == 'delivered'
    assert len(first['local_admissions']) == len(second['local_admissions']) == 1
    assert first['local_admissions'][0]['admission_id'] == second['local_admissions'][0]['admission_id']
    assert sum('sealed mixed result' in str(m.get('content')) for m in second['local_messages']) == 1
    assert len(events) == 3


@pytest.mark.asyncio
async def test_local_route_rotation_waits_for_admission_commit(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch, deliver='origin,local:LOCAL_TARGET,slack:C12345678:1700000777.000001',
            local=True, drift_at='serialize')
    async with peer() as (url, events):
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    assert first['row']['status'] == second['row']['status'] == 'delivered'
    assert first['serialized'] == [True]
    assert len(first['local_admissions']) == len(second['local_admissions']) == 1
    assert len(events) == 3
