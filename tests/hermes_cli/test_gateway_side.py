"""/bg and /btw on the shared gateway: the side task runs in the gateway (prompt.background /
prompt.btw) and its answer is routed off the event stream by task id, never built in the CLI."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli import gateway_side
from hermes_cli.gateway_connection import GatewayConnection

SID = "local-side"


class _Client:
    def __init__(self, text):
        self.events = asyncio.Queue()
        self.calls = []
        self.text = text

    async def rpc(self, method, *, _timeout=None, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return {"stored_session_id": SID, "subscription_id": "owner-side"}
        kind = "background.complete" if method == "prompt.background" else "btw.complete"
        # The answer can land before the verb's reply reaches the caller.
        await self.events.put({"jsonrpc": "2.0", "method": "event", "params": {
            "type": kind, "session_id": SID, "payload": {"task_id": "bg_1", "text": self.text}}})
        return {"task_id": "bg_1"}


def _connection(client):
    @asynccontextmanager
    async def connect():
        # A distinct socket has its own event queue; the owner waiter must not race
        # the UI connection's pump for one synthetic client's frames.
        socket = _Client(client.text)
        socket.calls = client.calls
        yield socket
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    return conn


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_side_task_runs_on_the_gateway_and_its_answer_is_routed_by_task_id(verb):
    client = _Client("DOG14 background answer")
    conn = _connection(client)
    try:
        task_id, wait = gateway_side.start(conn, SID, verb, "compute something")
        assert task_id == "bg_1"
        assert wait() == "DOG14 background answer"
        assert client.calls == [("session.resume", {"session_id": SID}),
                                (f"prompt.{verb}", {"session_id": SID, "text": "compute something"})]
        assert conn.events.empty()  # never left for the turn pump / idle tick to drop
    finally:
        conn.close()


def test_a_failed_side_task_raises_the_gateways_error():
    conn = _connection(_Client("error: model unavailable"))
    try:
        _task_id, wait = gateway_side.start(conn, SID, "background", "x")
        with pytest.raises(RuntimeError, match="model unavailable"):
            wait()
    finally:
        conn.close()


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_side_owner_subscription_outlives_ui_detach_and_closes_after_result(verb):
    sockets, closed = [], []

    @asynccontextmanager
    async def connect():
        socket = _Client("unused")
        sockets.append(socket)

        async def rpc(method, *, _timeout=None, **params):
            socket.calls.append((method, params))
            if method == "session.resume":
                return {"stored_session_id": SID, "subscription_id": "owner-side"}
            return {"task_id": "isolated-side"}

        socket.rpc = rpc
        try:
            yield socket
        finally:
            closed.append(socket)

    ui = GatewayConnection(connect=connect)
    ui.start(5)
    try:
        _, wait = gateway_side.start(ui, SID, verb, "inspect")
        assert len(sockets) == 2
        ui.call("session.detach", session_id=SID)
        owner = next(cell.cell_contents for cell in wait.wait.__closure__
                     if isinstance(cell.cell_contents, GatewayConnection))
        frame = {"method": "event", "params": {"type": f"{verb}.complete", "session_id": SID,
                 "payload": {"task_id": "isolated-side", "text": "error: result_not_durable\nORIGINAL_PRIVATE_RESULT"}}}
        owner.loop.call_soon_threadsafe(sockets[1].events.put_nowait, frame)
        with pytest.raises(gateway_side.SideResultError, match="ORIGINAL_PRIVATE_RESULT"):
            wait()
        assert wait.text == frame["params"]["payload"]["text"]
        assert owner.final and not owner._thread.is_alive()
        assert not ui.closed and sockets[1] in closed
    finally:
        ui.close()


@pytest.mark.parametrize("failed_method", ["session.resume", "prompt.background"])
def test_side_owner_socket_is_closed_when_admission_fails(failed_method):
    closed = []

    @asynccontextmanager
    async def connect():
        socket = _Client("")

        async def rpc(method, *, _timeout=None, **params):
            if method == failed_method:
                raise RuntimeError("owner refused")
            return {"stored_session_id": SID, "subscription_id": "owner-side"}

        socket.rpc = rpc
        try:
            yield socket
        finally:
            closed.append(socket)

    ui = GatewayConnection(connect=connect)
    try:
        with pytest.raises(RuntimeError, match="owner refused"):
            gateway_side.start(ui, SID, "background", "inspect")
        assert len(closed) == 1
    finally:
        ui.close()
