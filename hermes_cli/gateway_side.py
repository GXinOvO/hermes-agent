"""``/bg`` and ``/btw`` on the shared gateway session (``prompt.background`` / ``prompt.btw``).

On main the classic CLI built a second ``AIAgent`` (``/bg``) or made a one-shot auxiliary call
(``/btw``) in its own process. Rewired, the side work runs in the gateway, like Ink's: the verb
replies ``{task_id}`` at once and the answer arrives later on the session's event stream as
``background.complete`` / ``btw.complete`` ``{task_id, text}`` (``error: <exc>`` on failure,
``gateway/session_tui_side.py``). The connection's pump hands those frames to the waiter
registered here, so neither the turn pump nor the idle notification tick can drop them.
"""
from __future__ import annotations

import queue

SIDE_EVENTS = frozenset({"background.complete", "btw.complete"})
_POLL_SECONDS = 1.0


def connection_for(cli):
    """The CLI's gateway connection when its session is open, else None (not rewired). A hung-up
    connection is returned (its next call redials); only a final one is None."""
    from hermes_cli.gateway_tools_notify import gateway_store
    store = gateway_store(cli)
    if store is None or not getattr(cli, "_gateway_session_open", False) or store.connection.final:
        return None
    return store.connection


class SideResultError(RuntimeError):
    """A terminal event's full text, distinct from a transport exception."""

    def __init__(self, text):
        self.text = text
        super().__init__(text[len("error: "):])


class SideResultWait:
    """Callable result waiter carrying its immutable display owner and outcome."""

    def __init__(self, wait, session_id, task_id, *, profile_id=None, event_type=None):
        from hermes_constants import get_hermes_home
        self.wait = wait
        self.background_result = (profile_id or str(get_hermes_home().resolve()), session_id, task_id)
        self.event_type = event_type
        self.text = None
        self.error = None

    def __call__(self):
        try:
            self.text = self.wait()
            return self.text
        except Exception as exc:
            self.error = exc
            if isinstance(exc, SideResultError):
                self.text = exc.text
            raise


def start(connection, session_id, verb, text):
    """Admit the side task on the owner: ``(task_id, wait)``; ``wait()`` -> the answer (raises on failure)."""
    from hermes_constants import get_hermes_home
    profile_id = str(get_hermes_home().resolve())
    from hermes_cli.gateway_connection import GatewayConnection
    owner = None
    if isinstance(connection, GatewayConnection):
        # A UI session switch detaches its socket's subscription. Keep a separate owner
        # subscription until this task's event arrives, including a result that failed to seal.
        owner = GatewayConnection(connect=connection._connect)
        owner.launch_api_key = connection.launch_api_key
        try:
            owner.resume(owner.call, session_id)
        except Exception:
            owner.close()
            raise
        connection = owner
    # The epoch of the socket that admitted it: a redial before the waiter exists must still end it.
    try:
        result, epoch = connection.call_tagged(f"prompt.{verb}", session_id=session_id, text=text)
    except Exception:
        if owner is not None:
            owner.close()
        raise
    task_id = (result or {}).get("task_id")
    if not task_id:
        if owner is not None:
            owner.close()
        raise RuntimeError(f"prompt.{verb} returned no task id")
    inbox = connection.side_waiter(task_id)

    def wait():
        try:
            while True:
                try:
                    payload = inbox.get(timeout=_POLL_SECONDS)
                except queue.Empty:
                    # The task lived on the socket that admitted it: hung up, or already redialed.
                    if connection.closed or connection.epoch != epoch:
                        from hermes_cli.gateway_connection import DISCONNECTED
                        raise RuntimeError(DISCONNECTED) from None
                    continue
                answer = str((payload or {}).get("text") or "")
                if answer.startswith("error: "):
                    raise SideResultError(answer)
                return answer
        finally:
            connection.drop_side_waiter(task_id)
            if owner is not None:
                owner.close()
    return task_id, SideResultWait(wait, session_id, task_id, profile_id=profile_id,
                                  event_type=f'{verb}.complete')
