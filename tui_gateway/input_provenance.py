from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
import uuid


@dataclass
class _InboundInput:
    session_id: str
    text: str
    principal_id: str
    request_id: str
    message_uid: str
    received_at: float
    session: dict | None = None
    session_key: str | None = None
    consumed: bool = False
    open: bool = True


_inbound: ContextVar[_InboundInput | None] = ContextVar('legacy_interactive_input', default=None)


class InputProvenanceWriteError(RuntimeError):
    """A trusted Desktop input was accepted but its source receipt could not be stored."""


def _principal(transport):
    from gateway.owner_identity import host_owner_subject, principal_subject
    from gateway.session_identity import is_authenticated_identity

    identity = getattr(transport, 'auth_identity', None)
    if isinstance(identity, dict) and is_authenticated_identity(identity):
        if str(identity.get('user_id', '')).startswith('automation:'):
            return None
        return principal_subject(identity)
    if identity is None and getattr(transport, 'verified_operator', False) is True:
        return host_owner_subject()
    return None


@contextmanager
def inbound_input_scope(request, transport):
    value = None
    if isinstance(request, dict) and request.get('method') == 'prompt.submit':
        params = request.get('params')
        if (isinstance(params, dict) and isinstance(params.get('text'), str)
                and isinstance(params.get('session_id'), str)
                and params.get('display_kind') is None
                and not any(params.get(key) is not None for key in
                            ('_hosted_task', '_turn_author', '_hosted_terminal_callback', '_hosted_images'))):
            principal = _principal(transport)
            if (principal and not principal.startswith('automation:')
                    and getattr(transport, 'desktop_local', False) is True):
                from hermes_cli.input_sanitize import sanitize_user_prompt_text
                value = _InboundInput(params['session_id'], sanitize_user_prompt_text(params['text']),
                                      principal, uuid.uuid4().hex, uuid.uuid4().hex, time.time())
    token = _inbound.set(value)
    try:
        yield
    finally:
        if value is not None:
            value.open = False
        _inbound.reset(token)


def bind_input(session, text, display_kind):
    from tui_gateway import server

    value = _inbound.get()
    if (value is None or not value.open or value.consumed or value.session is not None
            or display_kind is not None or value.text != text):
        return
    with server._sessions_lock:
        if server._sessions.get(value.session_id) is session:
            value.session = session
            value.session_key = str(session.get('session_key') or '')


def take_input(session, text, display_kind):
    from tui_gateway import server

    value = _inbound.get()
    if (value is None or not value.open or value.consumed or display_kind is not None or value.text != text
            or value.session is not session or server._sessions.get(value.session_id) is not session):
        return None
    return value


def record_accepted_input(response):
    from tui_gateway import server
    from hermes_state_input_receipts import append_received_input

    value = _inbound.get()
    result = response.get('result', {}) if isinstance(response, dict) else {}
    if (value is None or value.consumed or not value.open or result.get('forwarded')
            or result.get('status') not in ('streaming', 'queued', 'steered', 'redirected')):
        return
    session = value.session
    if session is None or not value.session_key:
        return
    snapshot = {**session, 'session_key': value.session_key}
    with server._session_db(snapshot) as db:
        if db is None:
            return
        with server._sessions_lock:
            if server._sessions.get(value.session_id) is not session:
                return
            if db.get_session(value.session_key) is None:
                return
        message = {'role': 'user', 'content': value.text, 'timestamp': value.received_at,
                   'message_uid': value.message_uid}
        append_received_input(db, value.session_key, message,
                              principal_id=value.principal_id, request_id=value.request_id, active=False)
        value.consumed = True
