import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from cron import delivery_queue as queue, jobs, scheduler
from tests.gateway import frozen_live_http_peer as native


async def consume(home, platform, url, stop='none'):
    scratch = home / 'live-http-tmp'
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), TMPDIR=str(scratch))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).with_name('strict_live_http_fixture.py')), str(home), platform, url, stop,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await asyncio.wait_for(process.communicate(), 60)
    print('CRON_LIVE_HTTP_OWNER ' + json.dumps({'pid': process.pid, 'exit': process.returncode,
                                               'stdout': out.decode(), 'stderr': err.decode()}))
    assert process.returncode == (0 if stop in {'none', 'mutate'} else 88), err.decode()
    return json.loads(out) if out else None


def produce(home, platform):
    script = home / 'scripts' / 'live-http.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"live-http-producer"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("SEALED_LIVE_HTTP_RESULT")\n')
    chats = {'mattermost': ('channel-A', 'channel-B'), 'feishu': ('chat-A', 'chat-B'),
             'google_chat': ('spaces/space-A', 'spaces/space-B'), 'weixin': ('peer-A', 'peer-B')}[platform]
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
                          deliver=','.join(platform + ':' + chat for chat in chats))
    assert scheduler._run_one_job_body(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat', 'weixin'])
@pytest.mark.parametrize('stop,status,count', [('none', 'delivered', 2), ('after:0:done', 'delivered', 2),
    ('after:0:target_claimed', 'unknown', 0), ('before:0:done', 'unknown', 1), ('mutate', 'delivered', 2)])
async def test_live_http_cron_consumes_frozen_targets_across_fresh_owners(tmp_path, monkeypatch, platform, stop, status, count):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path, platform)
    async with native.peer() as wire:
        if stop not in {'none', 'mutate'}:
            await consume(tmp_path, platform, wire.base, stop)
        first = await consume(tmp_path, platform, wire.base, 'mutate' if stop == 'mutate' else 'none')
        trace = list(wire.events)
        second = await consume(tmp_path, platform, wire.base)
    assert first['row']['status'] == second['row']['status'] == status
    assert len(native.writes(wire)) == count
    assert all('MUTATED' not in e.body.decode('utf-8', 'ignore') for e in native.writes(wire))
    assert len(wire.events) == len(trace)
    assert (tmp_path / 'live-http-producer').read_text().splitlines() == ['executed']
    audit = json.loads(second['row']['thread_receipts_json'])
    assert [x['kind'] for x in audit['items']] == ['live-http', 'live-http']
    assert first['row']['content'] == '' and first['row']['job_json'] == '{}'
