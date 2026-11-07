"""Real Lark request models compiled before claim, submitted without SDK retry."""
import asyncio
import io
import json
from pathlib import Path
import uuid

from plugins.platforms import frozen_delivery as frozen


def _prepare(config, request, *, multipart=False):
    from lark_oapi.core import JSON
    from lark_oapi.core.http.transport import _build_header, _build_url
    from lark_oapi.core.model import RequestOption
    from lark_oapi.core.token import verify
    from lark_oapi.core.utils import Files
    from requests_toolbelt import MultipartEncoder
    import requests
    option = RequestOption()
    verify(config, request, option)
    if multipart:
        encoder = MultipartEncoder(Files.parse_form_data(request.body), boundary='hermes-frozen-media')
        body = encoder.to_string()
        option.headers['Content-Type'] = encoder.content_type
    else:
        body = JSON.marshal(request.body).encode('utf-8')
        option.headers['Content-Type'] = 'application/json; charset=utf-8'
    _build_header(request, option, config)
    url = _build_url(config.domain, request.uri, request.paths)
    prepared = requests.Request(request.http_method.name, url, params=request.queries).prepare()
    return frozen.request(prepared.url, body, dict(request.headers), 'feishu-message_id', account=str(config.app_id),
                          timeout=config.timeout or 30, method=request.http_method.name)


def _message(adapter, chat, kwargs, msg_type, payload, ordinal):
    metadata = kwargs.get('metadata') or {}
    thread = metadata.get('thread_id')
    reply = kwargs.get('reply_to') or (metadata.get('reply_to_message_id') if thread else None)
    # Deterministic SDK uuid survives a fresh-owner prepare of the same result.
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, frozen.digest(frozen.encode([chat, kwargs, msg_type, payload, ordinal]))))
    if reply:
        body = adapter._build_reply_message_body(content=payload, msg_type=msg_type, reply_in_thread=bool(thread), uuid_value=ident)
        return adapter._build_reply_message_request(reply, body), reply
    receive_id = thread or chat
    receive_type = 'thread_id' if thread else ('open_id' if chat.startswith('ou_') else 'chat_id')
    if not thread and chat.startswith('feishu_user_id:'):
        receive_id, receive_type = chat.split(':', 1)[1], 'user_id'
    body = adapter._build_create_message_body(receive_id=receive_id, msg_type=msg_type, content=payload, uuid_value=ident)
    return adapter._build_create_message_request(receive_type, body), reply or thread


async def freeze(adapter, state, calls, retained):
    calls = json.loads(frozen.encode(calls))
    loaded, media = frozen.snapshot_media(calls, retained)
    if adapter._client is None:
        raise ValueError('Feishu client unavailable')
    import copy
    config = copy.deepcopy(adapter._client.im.v1.message.config)
    requests, parents = [], []
    from plugins.platforms.feishu.adapter import _MARKDOWN_HINT_RE
    for method, kwargs in calls:
        if method == 'send':
            text = adapter.format_message(kwargs['content'])
            messages = [adapter._build_outbound_payload(chunk, prefer_post=bool(_MARKDOWN_HINT_RE.search(text)))
                        for chunk in adapter.truncate_message(text, adapter.MAX_MESSAGE_LENGTH)]
        else:
            path = str(kwargs[frozen.PATH_KEYS[method]])
            name = kwargs.get('file_name') or Path(path).name
            if method == 'send_image_file':
                image = io.BytesIO(loaded[path])
                image.name = name
                body = adapter._build_image_upload_body(image_type='message', image=image)
                model = adapter._build_image_upload_request(body)
                key, kind, tag = 'image_key', 'image', {'tag': 'img', 'image_key': frozen.UPLOAD_REF}
            else:
                upload_type, kind = adapter._resolve_outbound_file_routing(file_path=name, requested_message_type='audio' if method == 'send_voice' else 'file')
                if upload_type == 'opus':
                    raise ValueError('frozen Feishu Opus duration path is unqualified')
                body = adapter._build_file_upload_body(file_type=upload_type, file_name=name, file=io.BytesIO(loaded[path]))
                model = adapter._build_file_upload_request(body)
                key, tag = 'file_key', {'tag': 'media', 'file_key': frozen.UPLOAD_REF, 'file_name': name}
            compiled = await asyncio.to_thread(_prepare, config, model, multipart=True)
            from dataclasses import replace
            requests.append(replace(compiled, ack='feishu-' + key))
            if kwargs.get('caption'):
                messages = [('post', adapter._build_media_post_payload(caption=kwargs['caption'], media_tag=tag))]
            else:
                messages = [(kind, json.dumps({key: frozen.UPLOAD_REF}, ensure_ascii=False))]
        for kind, payload in messages:
            model, parent = _message(adapter, str(state.chat_id), kwargs, kind, payload, len(requests))
            parents.append(parent)
            requests.append(await asyncio.to_thread(_prepare, config, model))
    return frozen.plan('feishu', state, requests, media, account=str(config.app_id), parents=parents)


async def send(plan):
    requests = plan.consume()

    def submit():
        import requests as http
        uploaded, last_id = None, None
        with http.Session() as session:
            session.trust_env = False
            for request in requests:
                body = frozen.resolve_upload(request.body, uploaded)
                session.cookies.clear()
                response = session.request(request.method, request.url, headers=dict(request.headers), data=body,
                                           timeout=request.timeout, allow_redirects=False)
                if not 200 <= response.status_code < 300:
                    raise RuntimeError('Feishu frozen HTTP ACK unconfirmed: ' + str(response.status_code))
                result = frozen.acknowledge(request.ack, response.json())
                if request.ack in {'feishu-file_key', 'feishu-image_key'}:
                    uploaded = result
                else:
                    last_id = result
        return {'ok': True, 'message_id': last_id}
    return await asyncio.to_thread(submit)
