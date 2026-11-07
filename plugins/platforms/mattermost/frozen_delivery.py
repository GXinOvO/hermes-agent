"""Frozen Mattermost REST requests; no live adapter reads after preparation."""
import json
import mimetypes
from pathlib import Path

from plugins.platforms import frozen_delivery as frozen


async def freeze(adapter, state, calls, retained):
    calls = json.loads(frozen.encode(calls))
    loaded, media = frozen.snapshot_media(calls, retained)
    base = frozen.validate_url(str(adapter._base_url).rstrip('/'))
    token, account = str(adapter._token), str(adapter._bot_user_id or 'bot-token')
    if not token:
        raise ValueError('Mattermost authentication unavailable')
    headers = {'Authorization': 'Bearer ' + token}
    reply_mode = adapter._reply_mode
    requests, parents = [], []
    from plugins.platforms.mattermost.adapter import MAX_POST_LENGTH, _with_mentions_disabled
    for method, kwargs in calls:
        metadata = kwargs.get('metadata') or {}
        parent = kwargs.get('reply_to') or metadata.get('thread_id') or metadata.get('root_id') or getattr(state, 'thread_id', None)
        root = None
        if reply_mode == 'thread' and parent:
            import aiohttp
            from urllib.parse import quote
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(base + '/api/v4/posts/' + quote(str(parent), safe=''), headers=headers,
                                       allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if not 200 <= response.status < 300:
                        raise ValueError('Mattermost parent preflight failed')
                    resolved = await response.json()
                    root = resolved.get('root_id') or resolved.get('id')
            frozen.require_id(root, 'parent root')
        parents.append(root)
        if method == 'send':
            bodies = [_with_mentions_disabled({'channel_id': str(state.chat_id), 'message': chunk})
                      for chunk in adapter.truncate_message(adapter.format_message(kwargs['content']), MAX_POST_LENGTH)]
        else:
            path = str(kwargs[frozen.PATH_KEYS[method]])
            name = kwargs.get('file_name') or Path(path).name
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            body, content_type = frozen.multipart({'channel_id': str(state.chat_id)},
                [('files', (name, loaded[path], mime))])
            requests.append(frozen.request(base + '/api/v4/files', body,
                dict(headers, **{'Content-Type': content_type}), 'mattermost-upload', account=account, timeout=60))
            bodies = [_with_mentions_disabled({'channel_id': str(state.chat_id), 'message': kwargs.get('caption') or '',
                                              'file_ids': [frozen.UPLOAD_REF]})]
        for body in bodies:
            if root:
                body['root_id'] = root
            requests.append(frozen.json_request(base + '/api/v4/posts', body, headers, 'mattermost-message', account=account))
    return frozen.plan('mattermost', state, requests, media, account=account, parents=parents)


async def send(plan):
    import aiohttp
    requests = plan.consume()
    uploaded, last_id = None, None
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        for request in requests:
            body = frozen.resolve_upload(request.body, uploaded)
            async with session.request(request.method, request.url, data=body, headers=dict(request.headers),
                                       timeout=aiohttp.ClientTimeout(total=request.timeout), allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError('Mattermost frozen HTTP ACK unconfirmed: ' + str(response.status))
                payload = await response.json()
            result = frozen.acknowledge(request.ack, payload)
            if request.ack == 'mattermost-message':
                expected = json.loads(body)
                if (payload.get('channel_id') != expected['channel_id']
                        or payload.get('root_id', '') != expected.get('root_id', '')):
                    raise RuntimeError('Mattermost frozen ACK target mismatch')
            if request.ack.endswith('upload'):
                uploaded = result
            else:
                last_id = result
    return {'ok': True, 'message_id': last_id}
