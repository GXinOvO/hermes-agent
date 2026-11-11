from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web

from gateway import background_messaging_results as durable
from gateway import pending_final as pf
from gateway.config import Platform
from gateway.final_delivery import send_attachments, send_text
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_background import _key, _load
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.gateway.test_strict_sdk_delivery import peer, slack_adapter


@asynccontextmanager
async def image_server():
    state = {'fail': True, 'requests': 0}

    async def image(request):
        state['requests'] += 1
        if state['fail']:
            return web.Response(status=503)
        body = b'original-image' if state['requests'] == 2 else b'changed-image'
        return web.Response(body=body, content_type='image/png')

    app = web.Application()
    app.router.add_get('/image.png', image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        yield state, f'http://127.0.0.1:{port}/image.png'
    finally:
        await runner.cleanup()


def allow_loopback(monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                        lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))


@pytest.mark.asyncio
async def test_final_remote_image_preflight_precedes_text_claim(tmp_path, monkeypatch):
    allow_loopback(monkeypatch)
    async with image_server() as (image, url), peer() as wire, slack_adapter(wire) as adapter:
        with SessionDB(tmp_path / 'state.db') as db:
            db.create_session('sid', source='slack')
            epoch = begin_runtime_epoch(db, instance_id='owner')
            source = SessionSource(platform=Platform.SLACK, chat_id='C555', chat_type='dm')
            route = 'agent:main:slack:dm:C555'
            payload = {'text':'work', 'native_text_v1':{'route':route, 'source':source.to_dict(),
                'automation':{'owner':'sid', 'identity':'image-work', 'result_recovery_v1':'consume_only'}}}
            admit_session_input(db, epoch=epoch, principal_id='automation:' + route,
                                session_id='sid', request_id='image-work', payload=payload)
            row = claim_session_input(db, epoch=epoch, session_id='sid')
            event = MessageEvent(text='work', source=source, internal=True, message_id='image-work')
            response = f'sealed answer\n![image]({url})'
            authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
            async with automation_delivery_scope(authority, row, event):
                descriptor = await pf.prepare(authority, row, event, route, response)
            finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
                result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
            extracted = await adapter._extract_response_content(response, event, route, is_ephemeral_response=False)

            failed = await send_text(adapter, event, route, extracted.text_content,
                                     reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert not failed.success and 'pending' in failed.error
            assert wire.events == []
            assert {part['state'] for part in pf.pending(db)[0]['parts'].values()} == {'pending'}

            image['fail'] = False
            sent = await send_text(adapter, event, route, extracted.text_content,
                                   reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            outcomes = []
            await send_attachments(adapter, event, extracted, descriptor['metadata'],
                                   anything_sent=True, record_delivery=outcomes.append)
            assert sent.success and len(outcomes) == 1 and outcomes[0].success
            assert image['requests'] == 2
            assert any('original-image' in event['body'] for event in wire.events)
            assert not any('changed-image' in event['body'] for event in wire.events)
            assert {part['state'] for part in pf.pending(db)[0]['parts'].values()} == {'delivered'}


@pytest.mark.asyncio
async def test_background_remote_image_preflight_precedes_bundle_claim(tmp_path, monkeypatch):
    allow_loopback(monkeypatch)
    async with image_server() as (image, url), peer() as wire, slack_adapter(wire) as adapter:
        with SessionDB(tmp_path / 'state.db') as db:
            source = SessionSource(platform=Platform.SLACK, chat_id='C555')
            runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
            context = await durable.prepare(runner, 'work', source, 'remote-image', [], [])
            await durable.seal(context, 'remote-image', f'sealed answer\n![image]({url})')

            assert await durable.recover(runner, db) == 0
            assert wire.events == []
            assert db._execute_write(lambda conn: _load(conn, _key('remote-image')))['delivery'] == 'pending'

            image['fail'] = False
            assert await durable.recover(runner, db) == 1
            assert await durable.recover(runner, db) == 0
            assert image['requests'] == 2
            assert any('original-image' in event['body'] for event in wire.events)
            assert not any('changed-image' in event['body'] for event in wire.events)