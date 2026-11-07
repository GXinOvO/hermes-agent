"""Raft strict publication contract against a real loopback HTTP peer."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from aiohttp import web
import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, strict_transport_scope
from plugins.platforms.raft.adapter import RaftAdapter


@asynccontextmanager
async def peer(mode='sent'):
    wire = SimpleNamespace(events=[], release=asyncio.Event())

    async def handle(request):
        raw = await request.read()
        wire.events.append({'path': request.path, 'body': json.loads(raw),
                            'authorization': request.headers.get('Authorization')})
        if mode == 'drop':
            request.transport.abort()
            return web.Response()
        if mode == 'timeout':
            await wire.release.wait()
        if mode == '503':
            return web.json_response({'ok': False}, status=503)
        if mode == 'redirect':
            return web.Response(status=307, headers={'Location': wire.url + '/redirected'})
        ack = {'ok': True, 'state': 'sent', 'messageId': '11111111-2222-4333-8444-555555555555', 'messageSeq': 1}
        overrides = {'held': {'state': 'held'}, 'not_found': {'state': 'not_found'},
                     'committed': {'state': 'committed'}, 'missing_id': {'messageId': None},
                     'false_ok': {'ok': 1}, 'bad_seq': {'messageSeq': True},
                     'wrong_target': {'target': '#elsewhere'}, 'wrong_agent': {'agentId': 'foreign'},
                     'wrong_server': {'serverId': 'foreign'}}
        ack.update(overrides.get(mode, {}))
        return web.json_response(ack)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield wire
    finally:
        wire.release.set()
        await runner.cleanup()


def credential(tmp_path, monkeypatch, url, *, slug='inert'):
    root = tmp_path / slug
    root.mkdir(parents=True, exist_ok=True)
    value = {'apiKey': 'sk_agent_inert-test-only', 'agentId': 'agent-inert',
             'serverUrl': url, 'serverId': 'server-inert'}
    path = root / 'credential.json'
    path.write_text(json.dumps(value))
    monkeypatch.setenv('RAFT_PROFILE', slug)
    monkeypatch.setenv('RAFT_PROFILE_DIR', str(root))
    for name in ('SLOCK_PROFILE', 'SLOCK_PROFILE_DIR', 'RAFT_HOME', 'SLOCK_HOME',
                 'SLOCK_CLI_TRANSPORT_DIR', 'SLOCK_AGENT_LAUNCH_DIR'):
        monkeypatch.delenv(name, raising=False)
    return path, value


async def prepare(adapter, *, chat='#loopback', content='sealed-raft-loopback-result',
                  execution='completed-1', claim='completed-1:text', timeout=2, reply=None, metadata=None):
    hook = getattr(adapter, '_prepare_strict_transport', None)
    assert callable(hook), 'Raft strict pre-claim frozen publisher is missing'
    return await hook('send', chat_id=chat, content=content, reply_to=reply, metadata=metadata,
                      execution_id=execution, claim_id=claim, timeout_seconds=timeout)


async def consume(adapter, plan, *, chat='#loopback', content='sealed-raft-loopback-result',
                  execution='completed-1', claim='completed-1:text', reply=None, metadata=None):
    return await strict_call(adapter, 'send', chat_id=chat, content=content, reply_to=reply, metadata=metadata,
                             prepared_plan=plan, execution_id=execution, claim_id=claim)


@pytest.mark.asyncio
async def test_real_prepared_public_send_is_single_attempt(tmp_path, monkeypatch):
    async with peer() as wire:
        credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await prepare(adapter)
        assert wire.events == []
        result = await consume(adapter, plan)
        assert result.success, result.error
        assert len(wire.events) == 1
        assert wire.events[0]['path'] == '/internal/agent-api/v2/send'
        assert wire.events[0]['body']['target'] == '#loopback'
        assert wire.events[0]['body']['content'] == 'sealed-raft-loopback-result'
        assert wire.events[0]['authorization'] == 'Bearer sk_agent_inert-test-only'
        assert result.raw_response['server_confirmed_target'] is False
        with pytest.raises(RuntimeError, match='consumed'):
            await consume(adapter, plan)
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['503', 'drop', 'timeout', 'redirect', 'held', 'not_found', 'committed',
                                  'missing_id', 'false_ok', 'bad_seq', 'wrong_target', 'wrong_agent', 'wrong_server'])
async def test_unknown_ack_stops_without_retry_or_reconciliation(mode, tmp_path, monkeypatch):
    async with peer(mode) as wire:
        credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await prepare(adapter, timeout=0.15)
        result = await consume(adapter, plan)
        assert not result.success
        assert 'unknown' in result.error
        assert result.retryable is False
        assert len(wire.events) == 1
        with pytest.raises(RuntimeError, match='consumed'):
            await consume(adapter, plan)
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_ordinary_noop_keeps_cli_owned_delivery():
    adapter = RaftAdapter(PlatformConfig(enabled=True))
    assert (await adapter.send('aggregate-runtime', 'ordinary')).success
    with strict_transport_scope():
        assert not (await adapter.send('#loopback', 'unprepared')).success
