import asyncio

import pytest

from cron import delivery_queue as queue, scheduler
from plugins.platforms.slack import strict_delivery as slack_strict
from tests.cron.test_strict_continuable_thread import peer
from tests.gateway.test_strict_slack_thread import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('notify', [False, True])
async def test_new_thread_preserves_notification_policy_without_transport_reselection(tmp_path, monkeypatch, notify):
    async with peer() as (url, events):
        _, adapter = setup(tmp_path, monkeypatch, url)
        monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {
            'wrap_response': False, 'mirror_delivery': True, 'delivery': {'notify': notify}}})
        seen = []
        original = slack_strict.freeze_thread

        def freeze(adapter, state, calls, retained):
            seen.append({'notify': state.notify_delivery, 'metadata': [kwargs['metadata'] for _, kwargs in calls]})
            return original(adapter, state, calls, retained)

        monkeypatch.setattr(slack_strict, 'freeze_thread', freeze)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue,
            {adapter.platform: adapter}, asyncio.get_running_loop()) == 1
    assert queue.get_status('thread-exec')['status'] == 'delivered'
    assert len(seen) == 1 and seen[0]['notify'] is notify
    assert all(md['notify'] is notify for md in seen[0]['metadata'])
    assert len(events) == 2
    assert all('disable_notification' not in e['data'] for e in events)
    assert events[1]['data']['thread_ts'] == '1700000000.000001'
