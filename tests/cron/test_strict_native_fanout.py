import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from cron import delivery_queue as queue, jobs, scheduler
from tests.gateway import strict_native_thread_support as support


async def consume(home, platform, url, stop='none'):
    scratch = home / 'native-tmp'
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), TMPDIR=str(scratch))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).with_name('strict_native_fanout_fixture.py')), str(home), platform, url, stop,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await asyncio.wait_for(process.communicate(), 60)
    print('CRON_NATIVE_OWNER ' + json.dumps({'pid': process.pid, 'exit': process.returncode,
                                           'stdout': out.decode(), 'stderr': err.decode()}))
    assert process.returncode == (0 if stop == 'none' or stop.startswith('inbound:') else 88), err.decode()
    return json.loads(out) if out else None


def produce(home, platform):
    script = home / 'scripts' / 'native-result.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"native-producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("SEALED_NATIVE_CRON_RESULT")\n')
    parent = '!room:local' if platform == 'matrix' else '555'
    scope = {'discord': '444', 'telegram': '', 'matrix': 'local'}[platform]
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True, deliver='origin',
        attach_to_session=True,
        origin={'platform': platform, 'chat_id': parent, 'chat_type': 'group', 'scope_id': scope, 'user_id': 'human'})
    assert scheduler._run_one_job_body(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix'])
async def test_native_queue_uses_durable_create_body_and_seed(tmp_path, monkeypatch, platform):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path, platform)
    async with support.peer(platform) as wire:
        first = await consume(tmp_path, platform, wire.base)
        snapshot = list(wire.events)
        second = await consume(tmp_path, platform, wire.base)
    assert first['row']['status'] == second['row']['status'] == 'delivered'
    assert first['processed'] == 1 and second['processed'] == 0
    assert wire.events == snapshot
    assert len([event for event in wire.events if event['method'] != 'GET']) == 2
    assert (tmp_path / 'native-producer-calls').read_text().splitlines() == ['executed']
    audit = json.loads(second['row']['thread_receipts_json'])
    assert audit['items'][0]['platform'] == platform
    assert audit['items'][0]['root'] == ('$root' if platform == 'matrix' else '777')
    assert second['row']['thread_json'] is None and second['row']['content'] == ''
    transcripts = [entry['transcript'] for entry in first['sessions'].values()]
    assert any('SEALED_NATIVE_CRON_RESULT' in str(messages) for messages in transcripts)
