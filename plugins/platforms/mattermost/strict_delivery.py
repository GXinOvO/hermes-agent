"""Mattermost native chunks under an already-owned durable send claim."""
import logging

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


async def send_images(adapter, chat_id, images, metadata):
    if not images:
        return SendResult(success=False, error="no images to send")
    last_id = None
    try:
        for offset in range(0, len(images), 5):
            file_ids, captions = [], []
            for url, alt in images[offset:offset + 5]:
                loaded = await adapter._load_batch_image(url, len(file_ids))
                if loaded is None:
                    return SendResult(success=False, error="strict attachment bundle incomplete")
                file_id = await adapter._upload_file(chat_id, *loaded)
                if not file_id:
                    return SendResult(success=False, error="strict attachment upload ACK missing")
                file_ids.append(file_id)
                if alt:
                    captions.append(alt)
            data = await adapter._post_message(chat_id, "\n".join(captions), None, metadata, file_ids)
            if not data or not data.get('id'):
                return SendResult(success=False, error="strict attachment post ACK missing")
            last_id = data['id']
    except Exception as exc:
        logger.exception("Mattermost strict image bundle failed")
        return SendResult(success=False, error=str(exc) or type(exc).__name__)
    return SendResult(success=True, message_id=last_id)
