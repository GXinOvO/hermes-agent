"""Frozen iLink text and preclaimed local AES/CDN media requests."""
import json
import uuid

from plugins.platforms import frozen_delivery as frozen


async def freeze(adapter, state, calls, retained, *, saved_binding=None):
    calls = json.loads(frozen.encode(calls))
    # Validate the whole bundle before resolving any auth or submitting text.
    loaded, media = frozen.snapshot_media(calls, retained)
    if any(method != 'send' for method, _ in calls):
        from . import frozen_media
        return await frozen_media.freeze(adapter, state, calls, loaded, media, saved_binding=saved_binding)
    if saved_binding is not None and saved_binding.get('version') != 1:
        raise ValueError('Weixin saved binding transport version mismatch')
    base, token, account = str(adapter._base_url).rstrip('/'), str(adapter._token or ''), str(adapter._account_id)
    if not token:
        raise ValueError('Weixin authentication unavailable')
    context = adapter._token_store.get(account, str(state.chat_id))
    from plugins.platforms.weixin import adapter as native
    requests = []
    for _, kwargs in calls:
        text = adapter.format_message(kwargs['content'])
        # Cron supplies already-extracted calls. Inline extraction here could
        # silently choose an unfrozen secondary send path.
        files, _ = adapter.extract_media(text)
        images, _ = adapter.extract_images(text)
        if files or images:
            raise ValueError('Weixin frozen text requires pre-extracted media')
        for chunk in adapter._split_text(text):
            client_id = 'hermes-weixin-' + uuid.uuid5(uuid.NAMESPACE_URL,
                frozen.digest(frozen.encode([account, state.chat_id, kwargs, chunk, len(requests)]))).hex
            message = {'from_user_id': '', 'to_user_id': str(state.chat_id), 'client_id': client_id,
                       'message_type': native.MSG_TYPE_BOT, 'message_state': native.MSG_STATE_FINISH,
                       'item_list': [{'type': native.ITEM_TEXT, 'text_item': {'text': chunk}}]}
            if context:
                message['context_token'] = context
            body = json.dumps({'msg': message, 'base_info': {'channel_version': native.CHANNEL_VERSION}},
                              ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            headers = native._headers(token, body.decode('utf-8'))
            import base64
            headers['X-WECHAT-UIN'] = base64.b64encode(str(int(frozen.digest(body)[:8], 16)).encode('ascii')).decode('ascii')
            requests.append(frozen.request(base + '/' + native.EP_SEND_MESSAGE, body, headers, 'weixin-message', account=account))
    result = frozen.plan('weixin', state, requests, media, account=account, secrets=(context,))
    if saved_binding is not None and result['binding'] != saved_binding:
        raise ValueError('Weixin saved binding prepared transport drift')
    return result


async def send(plan):
    from . import frozen_media
    return await frozen_media.send(plan)


def validate_binding(binding, route):
    from .frozen_media_binding import validate
    return validate(binding, route)
