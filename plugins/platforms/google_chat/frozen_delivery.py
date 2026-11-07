"""Google discovery requests frozen to bytes and bearer headers before claim."""
import asyncio
import copy
import io
import json

from plugins.platforms import frozen_delivery as frozen


def _compile(model, credentials, account, ack):
    from google_auth_httplib2 import Request
    from plugins.platforms.google_chat.strict_delivery import SingleAttemptHttp
    credentials = copy.deepcopy(credentials)
    headers = dict(model.headers)
    credentials.before_request(Request(SingleAttemptHttp()), model.method, model.uri, headers)
    body = model.body or b''
    if isinstance(body, str):
        body = body.encode('utf-8')
    content_type = headers.get('content-type', headers.get('Content-Type', ''))
    if 'multipart/' in content_type:
        import re
        match = re.search(r'boundary="?([^";]+)', content_type)
        if not match:
            raise ValueError('Google multipart boundary missing')
        old = match.group(1).encode('ascii')
        boundary = ('hermes-' + frozen.digest(body.replace(old, b'BOUNDARY'))).encode('ascii')
        body = body.replace(old, boundary)
        for key in list(headers):
            if key.lower() == 'content-type':
                headers[key] = content_type.replace(old.decode('ascii'), boundary.decode('ascii'))
    headers.pop('content-length', None)
    headers.pop('Content-Length', None)
    return frozen.request(model.uri, body, headers, ack, account=account, method=model.method)


def _create(api, chat, body):
    kwargs = {'parent': chat, 'body': body}
    if body.get('thread'):
        kwargs['messageReplyOption'] = 'REPLY_MESSAGE_OR_FAIL'
    return api.spaces().messages().create(**kwargs)


async def freeze(adapter, state, calls, retained):
    calls = json.loads(frozen.encode(calls))
    loaded, media = frozen.snapshot_media(calls, retained)
    requests, parents, accounts = [], [], []
    from plugins.platforms.google_chat.adapter import _thread_body
    from googleapiclient.http import MediaIoBaseUpload
    chat = str(state.chat_id)
    # Select the existing account exactly once, before a claim. Refresh is
    # permitted only here and a failed selected identity never falls back.
    identity = adapter._last_sender_by_chat.get(chat)
    for method, kwargs in calls:
        thread = adapter._resolve_thread_id(kwargs.get('reply_to'), kwargs.get('metadata'), chat_id=chat)
        parents.append(thread)
        api, credentials, account = adapter._chat_api, adapter._credentials, 'bot'
        if method == 'send':
            bodies = [_thread_body(chunk, thread) for chunk in adapter._chunk_text(adapter.format_message(kwargs['content']))]
        else:
            if identity:
                api = adapter._user_chat_api_by_email.get(identity)
                credentials = adapter._user_creds_by_email.get(identity)
                account = identity
            else:
                api, credentials, account = adapter._user_chat_api, adapter._user_credentials, adapter._LEGACY_USER_IDENTITY
            if api is None or credentials is None:
                raise ValueError('selected Google Chat user OAuth identity unavailable')
            path = str(kwargs[frozen.PATH_KEYS[method]])
            from pathlib import Path
            name = kwargs.get('file_name') or Path(path).name
            mime = {'send_image_file': 'image/*', 'send_voice': 'audio/ogg', 'send_video': 'video/mp4'}.get(method, 'application/octet-stream')
            upload = api.media().upload(parent=chat, body={'filename': name},
                media_body=MediaIoBaseUpload(io.BytesIO(loaded[path]), mimetype=mime, resumable=False))
            requests.append(await asyncio.to_thread(_compile, upload, credentials, account, 'google-upload'))
            body = {'attachment': [{'attachmentDataRef': frozen.UPLOAD_REF}]}
            if kwargs.get('caption'):
                body['text'] = kwargs['caption']
            if thread:
                body['thread'] = {'name': thread}
            bodies = [body]
        if api is None or credentials is None:
            raise ValueError('selected Google Chat credentials unavailable')
        for body in bodies:
            requests.append(await asyncio.to_thread(_compile, _create(api, chat, body), credentials, account, 'google-message'))
        accounts.append(account)
    return frozen.plan('google_chat', state, requests, media, account=accounts, parents=parents)


async def send(plan):
    requests = plan.consume()

    def submit():
        from plugins.platforms.google_chat.strict_delivery import SingleAttemptHttp
        http = SingleAttemptHttp()
        uploaded, last_id = None, None
        for request in requests:
            response, content = http.request(request.url, method=request.method,
                body=frozen.resolve_upload(request.body, uploaded), headers=dict(request.headers))
            if not 200 <= response.status < 300:
                raise RuntimeError('Google Chat frozen HTTP ACK unconfirmed: ' + str(response.status))
            payload = json.loads(content)
            result = frozen.acknowledge(request.ack, payload)
            if request.ack == 'google-message':
                from urllib.parse import urlsplit
                parent = urlsplit(request.url).path.split('/spaces/', 1)[1].split('/messages', 1)[0]
                if not isinstance(result, str) or not result.startswith('spaces/' + parent + '/messages/'):
                    raise RuntimeError('Google Chat frozen ACK target mismatch')
            if request.ack == 'google-upload':
                uploaded = result
            else:
                last_id = result
        return {'ok': True, 'message_id': last_id}
    return await asyncio.to_thread(submit)
