from copy import deepcopy

import pytest

from hermes_cli import cli_background_results as side, gateway_side
from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.gateway_session_store import GatewaySessionStore
from tests.hermes_cli import test_gateway_side_result_failures as fixtures

viewer = fixtures.viewer
Connection = fixtures.Connection


def _waiter(cli, connection):
    cli._session_db = GatewaySessionStore(connection)
    cli._session_db._reader = connection
    _, wait = gateway_side.start(connection, "parent", connection.verb, "inspect")
    return wait


def _unavailable(*args, **kwargs):
    raise GatewayClientError("Gateway disconnected after snapshot display")


def _finish(cli, wait):
    thread = side.side_worker(cli, wait, name="late-side-result-test", fail_label="side",
                              header_lines=[], title_suffix="", empty_note="")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.mark.parametrize("verb", ["background", "btw"])
@pytest.mark.parametrize("text", ["error: original provider refusal", "ordinary successful result"])
@pytest.mark.parametrize("offline", [False, True])
def test_displayed_snapshot_deduplicates_late_waiter(viewer, tmp_path, verb, text, offline):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, text)
    wait = _waiter(cli, connection)
    side.display_results(cli)
    assert shown == [text]
    original_seen = set(cli._background_results_shown)
    if offline:
        connection.call = _unavailable
    _finish(cli, wait)
    assert sum(text in line for line in shown) == 1
    assert cli._background_results_shown == original_seen
    assert cli._background_results_pending == {}
    assert sum(name == f"prompt.{verb}" for name, _ in connection.calls) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
@pytest.mark.parametrize("text", ["different success", "error: different refusal",
                                 "error: result_not_durable\nONLY_IN_MEMORY_BODY", None])
def test_displayed_snapshot_preserves_distinct_or_unknown_live_result(viewer, tmp_path, monkeypatch, verb, text):
    from hermes_cli.gateway_connection import DISCONNECTED
    cli, shown = viewer
    connection = Connection(tmp_path, verb, text)
    connection.payload["text"] = "known canonical body"
    connection.snapshot["background_results"][0]["payload"] = deepcopy(connection.payload)
    wait = _waiter(cli, connection)
    side.display_results(cli)
    assert shown == ["known canonical body"]
    original_seen = set(cli._background_results_shown)
    connection.call = _unavailable
    if text is None:
        connection.inbox.get_nowait()
        connection.closed = True
        monkeypatch.setattr(gateway_side, "_POLL_SECONDS", 0.001)
    _finish(cli, wait)
    retained = text if text is not None else DISCONNECTED
    assert sum(retained in line for line in shown) == 1
    assert len(cli._background_results_pending) == 1
    assert cli._background_results_shown == original_seen
    side._display_pending_gateway_results(cli, [])
    assert sum(retained in line for line in shown) == 1
    assert sum(name == f"prompt.{verb}" for name, _ in connection.calls) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_displayed_event_is_not_changed_by_snapshot_mutation(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "original completed body")
    wait = _waiter(cli, connection)
    snapshot = connection.call("session.resume", session_id="parent")
    cli._session_db.snapshots["parent"] = snapshot
    side.display_results(cli)
    assert shown == ["original completed body"]
    snapshot["background_results"][0]["payload"]["text"] = "later unrelated value"
    connection.call = _unavailable
    _finish(cli, wait)
    assert sum("original completed body" in line for line in shown) == 1
    assert cli._background_results_pending == {}


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_unknown_snapshot_is_not_a_consumed_body_witness(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: unknown_execution; reconcile")
    connection.get_meta = lambda _key: '{"status":"unknown"}'
    wait = _waiter(cli, connection)
    side.display_results(cli)
    assert shown == [connection.payload["text"]]
    assert not cli._background_results_shown
    connection.call = _unavailable
    _finish(cli, wait)
    assert not cli._background_results_shown
    assert len(cli._background_results_pending) == 1
