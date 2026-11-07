import asyncio
import hashlib
import json

import pytest

from cron import delivery_queue as queue, jobs, scheduler
from tests.cron.test_strict_native_fanout import consume
from tests.gateway import strict_native_thread_support as support


def produce(home, platform, media):
    script = home / 'scripts' / 'native-media.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"native-media-producer"\n'
                      'with p.open("a") as f: f.write("executed\\n")\n'
                      'print("SEALED_NATIVE_MEDIA\\nMEDIA:' + str(media) + '")\n')
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True, deliver='origin',
        attach_to_session=True,
        origin={'platform': platform, 'chat_id': '555', 'chat_type': 'group',
                'scope_id': '444' if platform == 'discord' else '', 'user_id': 'human'})
    assert scheduler._run_one_job_body(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram'])
@pytest.mark.parametrize('mode', ['normal', 'changed-preflight', 'changed-after-claim'])
async def test_cron_native_durable_media_binding_precedes_root(tmp_path, monkeypatch, platform, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    media = tmp_path / 'report.bin'
    media.write_bytes(b'exact sealed native media')
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    await asyncio.to_thread(produce, tmp_path, platform, media)
    (tmp_path / 'native-settings.json').write_text(json.dumps({'mutate_after_claim':
        str(media) if mode == 'changed-after-claim' else None}))
    if mode == 'changed-preflight':
        media.write_bytes(b'different bytes before preflight')
    async with support.peer(platform) as wire:
        first = await consume(tmp_path, platform, wire.base)
        snapshot = list(wire.events)
        second = await consume(tmp_path, platform, wire.base)
    assert wire.events == snapshot
    if mode == 'changed-preflight':
        assert first['processed'] == second['processed'] == 0 and first['row']['status'] == 'pending'
        assert wire.events == [] and first['row']['owner_pid'] is None and first['row']['thread_json'] is None
    else:
        assert first['row']['status'] == second['row']['status'] == 'delivered'
        assert len(wire.events) == 3
        files = [value for value in wire.events[-1]['fields'].values() if isinstance(value, dict) and 'sha256' in value]
        assert files == [{'sha256': digest, 'filename': media.name}]
    assert (tmp_path / 'native-media-producer').read_text().splitlines() == ['executed']
