import asyncio
import logging

from gateway.platforms.base import SendResult
from gateway.strict_transport import strict_transport

logger = logging.getLogger(__name__)


async def send_text_chunk(sender, chat_id, text, reply_to, retry, group_code):
    from plugins.platforms.yuanbao.adapter import _text_elem
    retry = 1 if strict_transport() else retry
    last_error = 'Unknown error'
    for attempt in range(retry):
        try:
            if chat_id.startswith('group:'):
                msg_body = sender._build_msg_body_with_mentions(text, chat_id[len('group:'):])
            else:
                msg_body = [_text_elem(text)]
            raw = await sender._send_msg_body(chat_id, msg_body, reply_to, group_code)
            if raw.get('success'):
                return sender._to_send_result(raw)
            last_error = raw.get('error', 'Unknown error')
            logger.warning('[%s] send_text_chunk attempt %d/%d failed: %s', sender._adapter.name, attempt + 1, retry, last_error)
        except Exception as exc:
            last_error = str(exc)
            logger.warning('[%s] send_text_chunk attempt %d/%d exception: %s', sender._adapter.name, attempt + 1, retry, last_error, exc_info=True)
        if attempt < retry - 1:
            await asyncio.sleep(2 ** attempt)
    logger.error('[%s] send_text_chunk max retries (%d) exceeded. Last error: %s', sender._adapter.name, retry, last_error)
    return SendResult(success=False, error=f'Max retries exceeded: {last_error}')
