from contextlib import nullcontext
from aiohttp import web
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.strict_transport import strict_call


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_strict_github_comment_submits_once_without_redirect_or_cli_retry(tmp_path, monkeypatch, ack):
    posts = []

    async def receive(request):
        posts.append({'path':request.path, 'body':await request.json()})
        if not ack:
            request.transport.close()
        return web.json_response({'id':101}, status=201)

    app = web.Application()
    app.router.add_post('/repos/owner/repo/issues/7/comments', receive)
    peer = web.AppRunner(app)
    await peer.setup()
    site = web.TCPSite(peer, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
        adapter._profile_scope = lambda profile:nullcontext()
        adapter._github_env = lambda profile:{'GH_TOKEN':'loopback-only'}
        adapter._delivery_info['retained'] = {'deliver':'github_comment',
            'deliver_extra':{'repo':'owner/repo', 'pr_number':7}}
        monkeypatch.setattr('gateway.platforms.webhook_delivery.GITHUB_API', url, raising=False)
        result = await strict_call(adapter, 'send', chat_id='retained', content='sealed comment')
        assert result.success is ack
        assert posts == [{'path':'/repos/owner/repo/issues/7/comments', 'body':{'body':'sealed comment'}}]
    finally:
        await peer.cleanup()


@pytest.mark.asyncio
async def test_strict_github_uses_routed_environment_before_platform_submit(monkeypatch):
    from gateway.strict_transport import prepare_strict_transport
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
    scopes = []
    class Scope:
        def __enter__(self):
            scopes.append('secondary')
        def __exit__(self, *args):
            scopes.pop()
    adapter._profile_scope = lambda profile:Scope()
    def environment(profile):
        assert profile == 'secondary'
        assert scopes == ['secondary']
        return {'GH_TOKEN':'secondary-only'}
    adapter._github_env = environment
    adapter._delivery_info['retained'] = {'deliver':'github_comment', 'profile':'secondary',
        'deliver_extra':{'repo':'owner/repo', 'pr_number':7}}
    await prepare_strict_transport(adapter, 'send', chat_id='retained', content='answer')
    assert scopes == []
