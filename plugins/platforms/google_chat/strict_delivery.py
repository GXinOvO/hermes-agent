"""A per-request HTTP transport with no socket retry, redirect or auth replay."""
import http.client
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class SingleAttemptHttp:
    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        import httplib2
        url = urlsplit(uri)
        if url.scheme not in {'http', 'https'} or not url.hostname:
            raise ValueError('Invalid Google Chat request URL')
        connection = (http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection)(
            url.hostname, url.port, timeout=30)
        try:
            connection.request(method, url.path + ('?' + url.query if url.query else ''), body=body, headers=headers or {})
            response = connection.getresponse()
            result = httplib2.Response(dict(response.getheaders()))
            result.status = response.status
            result.reason = response.reason
            return result, response.read()
        finally:
            connection.close()

    def close(self):
        pass


def authed_http(credentials):
    from google_auth_httplib2 import AuthorizedHttp
    return AuthorizedHttp(credentials, http=SingleAttemptHttp(), max_refresh_attempts=0)


async def send_text(adapter, chat_id, content, reply_to, metadata):
    from gateway.platforms.base import SendResult
    from plugins.platforms.google_chat.adapter import _thread_body
    thread_id = adapter._resolve_thread_id(reply_to, metadata, chat_id=chat_id)
    chunks = adapter._chunk_text(adapter.format_message(content))
    result = SendResult(success=False, error='empty message')
    try:
        for chunk in chunks:
            result = await adapter._create_message(chat_id, _thread_body(chunk, thread_id))
            if not result.success:
                break
    except Exception as exc:
        logger.exception('Strict Google Chat final submission failed')
        return SendResult(success=False, error=str(exc))
    return result


async def send_file(adapter, api, identity, chat_id, path, filename, mime, caption, thread_id):
    import asyncio
    from gateway.platforms.base import SendResult
    from googleapiclient.http import MediaFileUpload
    from plugins.platforms.google_chat.adapter import _create_kwargs
    credentials = (adapter._user_credentials if identity == adapter._LEGACY_USER_IDENTITY
                   else adapter._user_creds_by_email.get(identity))
    if api is None or credentials is None:
        return SendResult(success=False, error='google_chat: native attachment requires user OAuth')

    def submit():
        upload = api.media().upload(parent=chat_id, body={'filename': filename},
                                   media_body=MediaFileUpload(path, mimetype=mime, resumable=False))
        uploaded = upload.execute(http=authed_http(credentials), num_retries=0)
        reference = uploaded.get('attachmentDataRef')
        if not reference:
            return SendResult(success=False, error='upload returned no attachmentDataRef')
        body = {'attachment': [{'attachmentDataRef': reference}]}
        body.update({k: v for k, v in (('text', caption), ('thread', {'name': thread_id} if thread_id else None)) if v})
        result = api.spaces().messages().create(**_create_kwargs(chat_id, body)).execute(
            http=authed_http(credentials), num_retries=0)
        adapter._track_outbound_thread(chat_id, result)
        return SendResult(success=True, message_id=result.get('name'))
    try:
        return await asyncio.to_thread(submit)
    except Exception as exc:
        logger.exception('Strict Google Chat attachment submission failed')
        return SendResult(success=False, error=str(exc))
