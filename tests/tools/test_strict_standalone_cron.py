"""Fresh-process native Cron queue drain through strict standalone and local HTTP."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.tools import test_strict_standalone_dispatch as fixtures


CHILD = r'''
import json, os, socket, sys
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from gateway.config import GatewayConfig, Platform, PlatformConfig
from cron import delivery_queue as queue, scheduler
from tests.tools.test_strict_standalone_dispatch import route
import gateway.config
from tools import send_message_strict as strict

home, platform, base, mode = sys.argv[1:]
os.environ['HERMES_HOME'] = home
os.environ['HOME'] = str(Path(home).parent / 'home')
Path(os.environ['HOME']).mkdir(exist_ok=True)
real_dns = socket.getaddrinfo
def local_dns(host, *args, **kwargs):
    if str(host) not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('external DNS forbidden in cold-start test')
    return real_dns(host, *args, **kwargs)
socket.getaddrinfo = local_dns
class Patches:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)
route(Patches(), SimpleNamespace(base=base), platform)
def forbidden(*args, **kwargs):
    raise AssertionError('completed result must not execute script or model')
scheduler._run_no_agent_job = forbidden
scheduler.run_job = forbidden
scheduler.load_config = lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}}
config = PlatformConfig(enabled=True, token=None if mode == 'notoken' else 'inert-preclaim')
gateway.config.load_gateway_config = lambda: GatewayConfig(platforms={Platform(platform): config})
queue.DELIVERY_DB = Path(home) / 'cron/deliveries.db'
chat = 'C555' if platform == 'slack' else '555'
content = 'sealed cold result'
if mode in ('missing', 'media', 'chunks', 'read_error'):
    file = Path(home) / 'first.txt'
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('sealed first')
    if mode == 'chunks':
        content = 'x' * 9000
    content += '\nMEDIA: ' + str(file)
    if mode in ('missing', 'read_error'):
        later = Path(home) / 'missing.txt'
        if mode == 'read_error':
            later.write_text('exists but cannot be read')
            original_open = Path.open
            def open_file(path, *args, **kwargs):
                if path == later:
                    raise OSError('injected later media read failure')
                return original_open(path, *args, **kwargs)
            Path.open = open_file
        content += '\nMEDIA: ' + str(later)
job = {'id': 'cold-job', 'execution_id': 'cold-exec', 'deliver': platform + ':' + chat}
if mode in ('thread', 'media', 'chunks'):
    job.update(deliver='origin', origin={'platform':platform, 'chat_id':chat, 'thread_id':'777', 'chat_type':'group'})
if queue.get_status('cold-exec') is None:
    queue.enqueue('cold-exec', job, content)
original_claim = queue.claim_next
claimed = []
def claim(**kwargs):
    row = original_claim(**kwargs)
    if row is not None:
        claimed.append(row['execution_id'])
        config.token = 'inert-after-claim'
        if mode in ('media', 'chunks'):
            file.write_text('changed after claim')
    return row
queue.claim_next = claim
count = scheduler.drain_delivery_queue({}, None)
again = scheduler.drain_delivery_queue({}, None)
row = queue.get_status('cold-exec')
print('COLD ' + json.dumps({'processed': count, 'again': again, 'claimed': claimed,
                          'status': row['status'], 'content': row.get('content'),
                          'pid': os.getpid(), 'home': home}))
'''


async def child(home, platform, base, mode):
    env = {'PATH': os.environ['PATH'], 'HOME': str(home.parent / 'home'), 'HERMES_HOME': str(home),
           'TMPDIR': str(home.parent), 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
           'PYTHONHASHSEED': '0', 'TZ': 'UTC', 'LANG': 'C.UTF-8'}
    command = [sys.executable, '-c', CHILD, str(home), platform, base, mode]
    run = await asyncio.to_thread(subprocess.run, command, env=env, cwd=Path(__file__).resolve().parents[2],
                                  capture_output=True, text=True, timeout=60, check=False)
    print('CHILD ' + json.dumps({'command': command, 'exit': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr}))
    assert run.returncode == 0, run.stdout + run.stderr
    return json.loads(next(line[5:] for line in run.stdout.splitlines() if line.startswith('COLD ')))


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
@pytest.mark.parametrize('fault', [None, 'lost', '503', '429', 'redirect'])
async def test_cold_cron_claim_precedes_one_real_submission_and_never_replays(tmp_path, platform, fault):
    async with fixtures.peer([fault]) as wire:
        home = tmp_path / 'state'
        first = await child(home, platform, wire.base, 'text')
        assert first['processed'] == 1 and first['again'] == 0
        assert first['status'] == ('delivered' if fault is None else 'unknown')
        assert first['claimed'] == ['cold-exec']
        assert len(wire.events) == 1
        if platform == 'telegram':
            assert wire.events[0]['path'].startswith('/botinert-preclaim/')
        else:
            assert wire.events[0]['authorization'].endswith('inert-preclaim')
        second = await child(home, platform, wire.base, 'text')
        assert second['pid'] != first['pid']
        assert second['processed'] == 0 and second['again'] == 0
        assert second['status'] == first['status']
        assert second['claimed'] == []
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
@pytest.mark.parametrize('mode', ['notoken', 'missing'])
async def test_cold_cron_static_refusal_retains_pending_without_claim(tmp_path, platform, mode):
    async with fixtures.peer() as wire:
        home = tmp_path / 'state'
        result = await child(home, platform, wire.base, mode)
        assert result['processed'] == 0 and result['again'] == 0
        assert result['claimed'] == []
        assert result['status'] == 'pending'
        assert result['content'].startswith('sealed cold result')
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
async def test_cold_cron_preserves_origin_thread_selected_before_claim(tmp_path, platform):
    async with fixtures.peer() as wire:
        result = await child(tmp_path / 'state', platform, wire.base, 'thread')
        assert result['processed'] == 1 and result['status'] == 'delivered'
        assert result['claimed'] == ['cold-exec'] and len(wire.events) == 1
        if platform == 'slack':
            assert wire.events[0]['json']['thread_ts'] == '777'
        elif platform == 'discord':
            assert wire.events[0]['path'] == '/channels/777/messages'
        else:
            assert wire.events[0]['form']['message_thread_id'] == ['777']



@pytest.mark.asyncio
async def test_cold_cron_telegram_unreadable_later_file_refuses_before_claim(tmp_path):
    async with fixtures.peer() as wire:
        result = await child(tmp_path / 'state', 'telegram', wire.base, 'read_error')
        assert result['processed'] == 0 and result['again'] == 0
        assert result['claimed'] == [] and result['status'] == 'pending'
        assert wire.events == []


@pytest.mark.asyncio
async def test_cold_cron_telegram_media_plan_is_frozen_before_claim(tmp_path):
    async with fixtures.peer() as wire:
        result = await child(tmp_path / 'state', 'telegram', wire.base, 'media')
        assert result['processed'] == 1 and result['status'] == 'delivered', result
        assert result['claimed'] == ['cold-exec']
        assert [event['path'] for event in wire.events] == [
            '/botinert-preclaim/sendMessage', '/botinert-preclaim/sendDocument']
        media = wire.events[1]['parts']
        assert next(part['body'] for part in media if part['name'] == 'document') == 'sealed first'
        assert next(part['body'] for part in media if part['name'] == 'message_thread_id') == '777'
        assert next(part['filename'] for part in media if part['name'] == 'document') == 'first.txt'
        assert 'changed after claim' not in json.dumps(wire.events)


@pytest.mark.asyncio
async def test_cold_cron_telegram_lost_chunk_ack_stops_files_and_restart(tmp_path):
    async with fixtures.peer([None, 'lost']) as wire:
        home = tmp_path / 'state'
        first = await child(home, 'telegram', wire.base, 'chunks')
        assert first['status'] == 'unknown' and first['claimed'] == ['cold-exec']
        assert len(wire.events) == 2
        assert all(event['path'] == '/botinert-preclaim/sendMessage' for event in wire.events)
        second = await child(home, 'telegram', wire.base, 'chunks')
        assert second['pid'] != first['pid']
        assert second['processed'] == 0 and second['claimed'] == [] and second['status'] == 'unknown'
        assert len(wire.events) == 2
