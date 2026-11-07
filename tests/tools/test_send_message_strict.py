from aiohttp import web
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.strict_transport import strict_transport_scope
from tools.send_message_tool import _send_to_platform


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['', 'disconnect', '503'])
async def test_strict_standalone_telegram_stops_after_lost_ack(tmp_path, monkeypatch, failure):
    posts = []
    text = 'sealed text ' * 150

    async def receive(request):
        data = await request.post()
        text = data.get('text')
        posts.append({'method':request.match_info['method'], 'text':text.strip() if text else None, 'document':data.get('document') is not None})
        if failure == 'disconnect':
            request.transport.close()
        if failure == '503':
            return web.json_response({'ok':False, 'error_code':503, 'description':'Service unavailable'}, status=503)
        return web.json_response({'ok':True, 'result':{'message_id':len(posts), 'chat':{'id':123,'type':'private'}, 'date':1}})

    app = web.Application()
    app.router.add_post('/{token}/{method}', receive)
    peer = web.AppRunner(app)
    await peer.setup()
    site = web.TCPSite(peer, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    monkeypatch.setattr('tools.send_message_strict.TELEGRAM_API', url)

    doc = tmp_path / 'report.txt'
    doc.write_text('sealed file')
    from tools import send_message_strict as strict
    config = PlatformConfig(token='loopback-only')
    media = [(str(doc), False)]
    plan = strict.prepare(Platform.TELEGRAM, config, '123', text, media_files=media,
                          execution_id='standalone-test', claim_id='local-claim')
    try:
        with strict_transport_scope():
            result = await _send_to_platform(Platform.TELEGRAM, config,
                '123', text, media_files=media, prepared_plan=plan,
                execution_id='standalone-test', claim_id='local-claim')
        assert bool(result.get('success')) is (not failure)
        assert posts == ([{'method':'sendMessage','text':text.strip(),'document':False},
                          {'method':'sendDocument','text':None,'document':True}] if not failure else
                         [{'method':'sendMessage','text':text.strip(),'document':False}])
    finally:
        await peer.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
async def test_dispatcher_preserves_prepared_claim_binding(monkeypatch, platform):
    from tools import send_message_strict as strict
    from tests.tools import test_strict_standalone_dispatch as fixtures
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        target = fixtures.target(platform)
        plan = strict.prepare(platform, config, target, 'sealed', thread_id='777',
                              execution_id='execution', claim_id='claim')
        with strict_transport_scope():
            rejected = await _send_to_platform(Platform(platform), config, target, 'sealed',
                thread_id='777', prepared_plan=plan, execution_id='execution', claim_id='other')
            assert rejected.get('error') and wire.events == []
            accepted = await _send_to_platform(Platform(platform), config, target, 'sealed',
                thread_id='777', prepared_plan=plan, execution_id='execution', claim_id='claim')
            duplicate = await _send_to_platform(Platform(platform), config, target, 'sealed',
                thread_id='777', prepared_plan=plan, execution_id='execution', claim_id='claim')
        assert accepted.get('success') and duplicate.get('ambiguous') is True
        assert len(wire.events) == 1
