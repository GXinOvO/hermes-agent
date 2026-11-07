import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from cron import delivery_queue as queue, jobs, scheduler
from tests.gateway import frozen_live_http_peer as native
from tests.gateway import test_frozen_weixin_media_responses as cdn
from plugins.platforms.weixin import adapter as wx


async def consume(home, url, stop='none'):
    scratch = home / 'weixin-media-tmp'
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), TMPDIR=str(scratch),
               PYTHONDONTWRITEBYTECODE='1')
    process = await asyncio.create_subprocess_exec(sys.executable, '-B',
        str(Path(__file__).with_name('strict_weixin_media_fixture.py')), str(home), url, stop,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == (0 if stop in {'none', 'mutate'} else 88), err.decode()
    print('WEIXIN_CRON_OWNER ' + json.dumps({'pid': process.pid, 'exit': process.returncode,
                                           'stderr': err.decode()}))
    return json.loads(out) if out else None


def produce(home):
    script = home / 'scripts' / 'weixin-media.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"weixin-media-producer"\n'
        'with p.open("a") as f: f.write("executed\\n")\n'
        'print("SEALED_WEIXIN_MEDIA\\nMEDIA:"+str(Path(__file__).parents[1]/"proof.bin"))\n')
    (home / 'proof.bin').write_bytes(b'ORIGINAL_MEDIA_BYTES')
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
                          deliver='weixin:peer-A,weixin:peer-B')
    assert scheduler._run_one_job_body(job)


def snapshots(home, name):
    path = home / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,status,writes', [
    ('none', 'delivered', 8), ('after_claim', 'delivered', 8),
    ('after:0:done', 'delivered', 8), ('after:0:target_claimed', 'unknown', 0),
    ('before:0:done', 'unknown', 4), ('mutate', 'delivered', 8)])
async def test_weixin_crypto_envelope_survives_fresh_cron_owner(tmp_path, monkeypatch, stop, status, writes):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path)
    async with native.peer() as wire:
        if stop not in {'none', 'mutate'}:
            await consume(tmp_path, wire.base, stop)
        first = await consume(tmp_path, wire.base, 'mutate' if stop == 'mutate' else 'none')
        trace = list(wire.events)
        second = await consume(tmp_path, wire.base)
    assert first['row']['status'] == second['row']['status'] == status
    assert len(native.writes(wire)) == writes
    assert len(wire.events) == len(trace)
    assert (tmp_path / 'weixin-media-producer').read_text().splitlines() == ['executed']
    claims = snapshots(tmp_path, 'claim-snapshots')
    assert claims, first
    bindings = [entry['binding'] for entry in claims]
    assert all(value == bindings[0] for value in bindings)
    for target in bindings[0]['targets']:
        binding = target['plan']
        assert binding['version'] == 2 and binding['crypto_context']['ciphertext']
        assert binding['identity']['execution'] == bindings[0]['job']['execution_id']
        assert binding['identity']['target']['chat_id'] == binding['actualdestination']
        assert binding['media'] == [[str(tmp_path / 'proof.bin'), False,
                                      hashlib.sha256(b'ORIGINAL_MEDIA_BYTES').hexdigest(), 'proof.bin']]
    allocations = [event for event in wire.events if event.path.endswith('/getuploadurl')]
    uploads = [event for event in wire.events if event.path == '/cdn-upload']
    for allocation, upload in zip(allocations, uploads):
        assert wx._aes128_ecb_decrypt(upload.body, bytes.fromhex(allocation.payload['aeskey'])) == b'ORIGINAL_MEDIA_BYTES'
    public = json.dumps(bindings)
    assert 'synthetic-weixin-A' not in public and 'synthetic-context-A' not in public
    audit = json.loads(second['row']['thread_receipts_json'])
    assert [item['stage'] for item in audit['items']] == (
        ['done', 'done'] if status == 'delivered' else ['target_claimed', 'initial'])
    assert second['row']['content'] == '' and second['row']['job_json'] == '{}'


@pytest.mark.asyncio
async def test_unknown_cdn_ack_prevents_later_media_and_later_target(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path)
    async with native.peer(lambda event, index: 'lost' if event.path == '/cdn-upload' else None) as wire:
        first = await consume(tmp_path, wire.base)
        trace = list(wire.events)
        second = await consume(tmp_path, wire.base)
    assert first['row']['status'] == second['row']['status'] == 'unknown'
    assert [event.path.rsplit('/', 1)[-1] for event in native.writes(wire)] == ['sendmessage', 'getuploadurl', 'cdn-upload']
    assert wire.events == trace
    assert (tmp_path / 'weixin-media-producer').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
async def test_media_changed_since_producer_refuses_all_targets_before_claim(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path)
    (tmp_path / 'proof.bin').write_bytes(b'CHANGED_BEFORE_CLAIM')
    async with native.peer() as wire:
        first = await consume(tmp_path, wire.base)
    assert first['row']['status'] == 'pending' and first['processed'] == 0
    assert first['row']['thread_json'] is None and native.writes(wire) == []
    assert not snapshots(tmp_path, 'claim-snapshots')


@pytest.mark.asyncio
async def test_parent_timeout_cancels_current_send_before_later_wire(tmp_path, monkeypatch):
    from cron.strict_thread_delivery import send_target
    from plugins.platforms.weixin import frozen_delivery as delivery
    gate = type('Gate', (), {'index': 1, 'accepted': asyncio.Event(), 'release': asyncio.Event()})()
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'ORIGINAL_MEDIA_BYTES')
    monkeypatch.setattr('cron.scheduler_delivery._get_standalone_send_timeout', lambda: 0.05)
    async with cdn.cdn_peer(gate=gate) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        state = native.state_for('weixin', chat)
        state.loop = asyncio.get_running_loop()
        plan = await delivery.freeze(adapter, state, [('send_document', {'file_path': str(path)})], [(str(path), False)])
        frozen = {'kind': 'live-http', '_frozen_http_platform': 'weixin', 'plan': plan}
        finished = asyncio.Event()
        original = delivery.send
        async def observed(value):
            try:
                return await original(value)
            finally:
                finished.set()
        monkeypatch.setattr(delivery, 'send', observed)
        sending = asyncio.create_task(asyncio.to_thread(send_target, state, frozen))
        await asyncio.wait_for(gate.accepted.wait(), 5)
        with pytest.raises(TimeoutError):
            await sending
        gate.release.set()
        await asyncio.wait_for(finished.wait(), 5)
        assert len(wire.events) == 1
        with pytest.raises(RuntimeError, match='already attempted'):
            await delivery.send(plan)
