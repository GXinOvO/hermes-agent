import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from plugins.platforms.weixin import frozen_delivery as helper
from tests.gateway import frozen_live_http_peer as native


@asynccontextmanager
async def selected_peer(userinfo):
    wire = SimpleNamespace(events=[])
    async def handle(request):
        raw = await request.read()
        body = json.loads(raw) if request.path.endswith(('/getuploadurl', '/sendmessage')) else None
        wire.events.append({'path': request.path, 'authorization_present': 'Authorization' in request.headers,
                            'body_bytes': len(raw)})
        if request.path.endswith('/getuploadurl'):
            url = wire.base.replace('http://', 'http://' + userinfo) + '/c2c/upload?encrypted_query_param=inert&filekey=' + body['filekey']
            return web.json_response({'ret': 0, 'upload_full_url': url})
        if request.path == '/c2c/upload':
            return web.Response(headers={'x-encrypted-param': 'safe-reference'})
        return web.json_response({'ret': 0})
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield wire
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('userinfo', ['@', ':@', 'user@'])
async def test_empty_and_nonempty_userinfo_refuses_cdn_followup(tmp_path, userinfo):
    path = tmp_path / 'review.bin'
    path.write_bytes(b'SYNTHETIC_REVIEW_BYTES')
    async with selected_peer(userinfo) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        plan = await helper.freeze(adapter, native.state_for('weixin', chat),
            [('send_document', {'file_path': str(path)})], [(str(path), False)])
        error = None
        try:
            await helper.send(plan)
        except Exception as exc:
            error = type(exc).__name__
        print('PROBE_USERINFO ' + json.dumps({'case': userinfo, 'error': error, 'events': wire.events}))
        assert len(wire.events) == 1, 'selected CDN policy says every userinfo form must be rejected before upload'


@pytest.mark.asyncio
async def test_public_saved_template_and_wire_match_except_declared_response_fields(tmp_path):
    import base64
    from plugins.platforms import frozen_delivery as shared
    from tests.gateway import test_frozen_weixin_media_responses as cdn
    path = tmp_path / 'review.bin'
    path.write_bytes(b'SYNTHETIC_REVIEW_BYTES')
    async with cdn.cdn_peer('full') as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        state = native.state_for('weixin', chat)
        state.execution_id = 'independent-wire'
        plan = await helper.freeze(adapter, state,
            [('send_document', {'file_path': str(path), 'caption': 'frozen-caption'})], [(str(path), False)])
        binding = plan['binding']
        assert wire.events == []
        result = await helper.send(plan)
        allocation, upload, caption, publication = wire.events
        aes_hex = allocation.payload['aeskey']
        filekey = allocation.payload['filekey']
        secrets = ['synthetic-context-A', filekey, aes_hex, base64.b64encode(aes_hex.encode()).decode()]
        for index, event in [(0, allocation), (2, caption), (3, publication)]:
            body = event.body
            if index == 3:
                body = body.replace(json.dumps('quoted-"-reference\\tail', ensure_ascii=False).encode(), json.dumps(shared.UPLOAD_REF).encode())
            assert hashlib_sha(body) == binding['prepared'][index]['wire_body_sha256']
            for secret in secrets:
                body = body.replace(json.dumps(secret, ensure_ascii=False)[1:-1].encode(),
                    ('sha256:' + hashlib_sha(secret.encode())).encode())
            assert body == base64.b64decode(binding['prepared'][index]['body_b64'])
        assert hashlib_sha(upload.body) == binding['prepared'][1]['body_sha256']
        assert len(upload.body) == binding['prepared'][1]['bytes']
        assert result['message_id'] == publication.payload['msg']['client_id']
        assert 'Authorization' not in upload.headers
        print('PROBE_WIRE_MATCH ' + json.dumps({'requests': len(wire.events), 'template_body_hashes_match': True,
            'ciphertext_hash_match': True, 'ack_source': 'frozen-client-id', 'response_slots': ['upload_url', 'encrypted_query_param']}))


def hashlib_sha(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()
