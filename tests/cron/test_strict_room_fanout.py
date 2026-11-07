import pytest

from gateway import hosted_rooms
from tests.cron.test_strict_mixed_fanout import enqueue, owner, peer


def room(home):
    store = hosted_rooms.default_db_path()
    hosted_rooms.create_room(store, room_id='room-cron', name='Feed',
        members=[{'member_id': 'member-alpha', 'profile': 'alpha', 'handle': 'alpha'}],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id())
    return store


def posts(store):
    return [event for event in hosted_rooms.read_events(store, room_id='room-cron')['events']
            if event['kind'] == 'message.user']


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,status,posted,sends', [
    ('none', 'delivered', 1, 3),
    ('after:1:target_claimed', 'unknown', 0, 2),
    ('before:1:done', 'unknown', 1, 2),
    ('after:1:done', 'delivered', 1, 3)])
async def test_room_fanout_uses_one_saved_post_and_prefix(tmp_path, monkeypatch, stop, status, posted, sends):
    enqueue(tmp_path, monkeypatch, deliver='origin,room:room-cron,slack:C12345678:1700000777.000001')
    store = room(tmp_path)
    async with peer() as (url, events):
        if stop != 'none':
            await owner(tmp_path, url, stop)
        first = await owner(tmp_path, url)
        wire = list(events)
        second = await owner(tmp_path, url)
    assert first['row']['status'] == second['row']['status'] == status
    assert len(posts(store)) == posted and len(events) == sends and events == wire
    if posted:
        assert posts(store)[0]['payload']['text'] == 'sealed mixed result'
        assert posts(store)[0]['actor']['id'] == 'cron:fanout-job'


@pytest.mark.asyncio
async def test_invalid_later_room_preflight_spends_no_claim_or_send(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch, deliver='origin,room:missing')
    async with peer() as (url, events):
        result = await owner(tmp_path, url)
    assert not events and result['row']['status'] == 'pending'
    assert result['row']['thread_json'] is None and not result['row']['owner_pid']


@pytest.mark.asyncio
async def test_room_authority_change_after_claim_stops_before_post(tmp_path, monkeypatch):
    from gateway.hosted_room_replicas import demote_room
    enqueue(tmp_path, monkeypatch, deliver='origin,room:room-cron,slack:C12345678:1700000777.000001')
    store = room(tmp_path)
    async with peer() as (url, events):
        await owner(tmp_path, url, 'after_claim')
        state = hosted_rooms.room_state(store, room_id='room-cron')
        demote_room(store, room_id='room-cron', observed_gateway_id=state['authority_gateway_id'] + ':new',
                    observed_epoch=state['authority_epoch'] + 1)
        one = await owner(tmp_path, url)
    assert one['row']['status'] in {'pending', 'unknown'}
    assert posts(store) == [] and events == []
