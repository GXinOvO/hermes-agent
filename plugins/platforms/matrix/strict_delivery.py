"""Private one-attempt mautrix transport for completed-result delivery.

Human traffic keeps its configured SDK/session retries. Each strict operation gets
its own HTTPAPI and session: neither SDK backoff nor aiohttp's idempotent PUT replay
may resubmit a possibly accepted Matrix event. E2EE is deliberately unverified.
"""
from copy import copy
from functools import wraps

from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport, strict_transport


class _NoRedirectSession:
    def __init__(self, session):
        self.session = session

    def request(self, *args, **kwargs):
        kwargs['allow_redirects'] = False
        return self.session.request(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.session, name)


def strict_matrix_method(method):
    @wraps(method)
    @single_attempt_transport
    async def send(adapter, *args, **kwargs):
        if not strict_transport():
            return await method(adapter, *args, **kwargs)
        client = adapter._client
        if client is None:
            return SendResult(success=False, error='Matrix not connected')
        if adapter._encryption or getattr(client, 'crypto', None):
            return SendResult(success=False, error='strict Matrix E2EE transport unverified')
        from mautrix.api import HTTPAPI
        from mautrix.client import Client
        from plugins.platforms.matrix.adapter import _create_matrix_session
        api = client.api
        if not isinstance(api, HTTPAPI):
            return SendResult(success=False, error='strict Matrix HTTPAPI unverified')
        async with _create_matrix_session(adapter._proxy_url) as session:
            # Per-operation session: disabling the PUT replay cannot affect human sends.
            if not hasattr(session, '_retry_connection'):
                return SendResult(success=False, error='strict Matrix aiohttp retry control unavailable')
            session._retry_connection = False
            private_api = HTTPAPI(base_url=api.base_url, token=api.token,
                                  client_session=_NoRedirectSession(session), default_retry_count=0)
            original_send = private_api._send

            async def checked_send(*a, **kw):
                data, response = await original_send(*a, **kw)
                if response.status >= 300:
                    raise RuntimeError('Matrix strict redirect refused')
                return data, response

            private_api._send = checked_send
            private = copy(adapter)
            private._client = Client(mxid=client.mxid, api=private_api)
            return await method(private, *args, **kwargs)
    return send
