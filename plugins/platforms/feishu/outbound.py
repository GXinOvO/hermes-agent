"""Feishu send policy; SDK transport submits each request once."""
import asyncio
import json
import logging
from typing import Any, Optional

from gateway.platforms.base import SendResult, describe_proxy_error, env_proxy_for_redaction
from gateway.platforms.helpers import send_chunks
from gateway.strict_transport import single_attempt_transport, strict_transport

logger = logging.getLogger(__name__)


class FeishuOutboundMixin:
    @single_attempt_transport
    async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
        if not strict_transport():
            return await super().send_multiple_images(chat_id, images, metadata, human_delay)
        from gateway.strict_transport import require_strict_transport
        from urllib.request import url2pathname
        calls = []
        for url, caption in images:
            if url.startswith("file://"):
                method, kwargs = "send_image_file", {"image_path": url2pathname(url[7:])}
            elif self._is_animation_url(url):
                method, kwargs = "send_animation", {"animation_url": url}
            else:
                method, kwargs = "send_image", {"image_url": url}
            require_strict_transport(self, method)
            calls.append((method, dict(kwargs, caption=caption or None)))
        result = SendResult(success=False, error="no images to send")
        for method, kwargs in calls:
            result = await getattr(self, method)(chat_id=chat_id, metadata=metadata, **kwargs)
            if not result.success:
                break
        return result

    @single_attempt_transport
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        from plugins.platforms.feishu.adapter import _MARKDOWN_HINT_RE, _POST_CONTENT_INVALID_RE, _strip_markdown_to_plain_text
        if not self._client:
            return SendResult(success=False, error="Not connected")
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
        prefer_post = bool(_MARKDOWN_HINT_RE.search(formatted))

        async def plain(chunk):
            return await self._feishu_send_with_retry(chat_id=chat_id, msg_type="text",
                payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
                reply_to=reply_to, metadata=metadata)

        async def send_one(chunk):
            msg_type, payload = self._build_outbound_payload(chunk, prefer_post=prefer_post)
            try:
                response = await self._feishu_send_with_retry(chat_id=chat_id, msg_type=msg_type,
                    payload=payload, reply_to=reply_to, metadata=metadata)
            except Exception as exc:
                if strict_transport() or msg_type != "post" or not _POST_CONTENT_INVALID_RE.search(str(exc)):
                    raise
                logger.warning("[Feishu] Invalid post payload rejected by API; falling back to plain text")
                response = await plain(chunk)
            if (not strict_transport() and msg_type == "post" and not self._response_succeeded(response)
                    and _POST_CONTENT_INVALID_RE.search(str(getattr(response, "msg", "") or ""))):
                logger.warning("[Feishu] Post payload rejected by API response; falling back to plain text")
                response = await plain(chunk)
            return self._finalize_send_result(response, "send failed")

        try:
            return await send_chunks(chunks, send_one)
        except Exception as exc:
            from plugins.platforms.feishu.adapter import _onboard_open_base_url
            shown = describe_proxy_error(exc, env_proxy_for_redaction(_onboard_open_base_url(self._domain_name)))
            logger.error("[Feishu] Send error: %s", shown or exc, exc_info=not shown)
            return SendResult(success=False, error=shown or str(exc))

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to, metadata):
        from plugins.platforms.feishu.adapter import (
            _FEISHU_SEND_ATTEMPTS, _FEISHU_REPLY_FALLBACK_CODES, _POST_CONTENT_INVALID_RE, _onboard_open_base_url,
        )
        if strict_transport():
            return await self._send_raw_message(chat_id=chat_id, msg_type=msg_type, payload=payload,
                                                reply_to=reply_to, metadata=metadata)
        active_reply_to = reply_to
        for attempt in range(_FEISHU_SEND_ATTEMPTS):
            try:
                response = await self._send_raw_message(chat_id=chat_id, msg_type=msg_type, payload=payload,
                                                        reply_to=active_reply_to, metadata=metadata)
                if (active_reply_to and not self._response_succeeded(response)
                        and getattr(response, "code", None) in _FEISHU_REPLY_FALLBACK_CODES):
                    if (metadata or {}).get("thread_id"):
                        return response
                    logger.warning("[Feishu] Reply target unavailable; falling back to new message in chat %s", chat_id)
                    active_reply_to = None
                    response = await self._send_raw_message(chat_id=chat_id, msg_type=msg_type, payload=payload,
                                                            reply_to=None, metadata=metadata)
                return response
            except Exception as exc:
                if (msg_type == "post" and _POST_CONTENT_INVALID_RE.search(str(exc))) or attempt >= _FEISHU_SEND_ATTEMPTS - 1:
                    raise
                wait_seconds = 2 ** attempt
                logger.warning("[Feishu] Send attempt %d/%d failed for chat %s; retrying in %ds: %s",
                               attempt + 1, _FEISHU_SEND_ATTEMPTS, chat_id, wait_seconds,
                               describe_proxy_error(exc, env_proxy_for_redaction(_onboard_open_base_url(self._domain_name))) or exc)
                await asyncio.sleep(wait_seconds)
        raise RuntimeError("Feishu send failed")
