"""Classic side-result errors share the original owner and recovery display identity."""
from copy import deepcopy
import json
import queue
from types import SimpleNamespace

import pytest

from hermes_cli import cli_background_results as side, cli_commands_mixin as commands, gateway_side
from hermes_cli.gateway_session_store import GatewaySessionStore


class Connection:
    def __init__(self, profile, verb, text):
        self.epoch = 1
        self.closed = False
        self.final = False
        self.verb = verb
        self.payload = {"task_id": "side_original", "text": text}
        self.snapshot = {
            "stored_session_id": "parent", "info": {"profile_id": str(profile)},
            "background_results": [{"event_type": f"{verb}.complete", "payload": deepcopy(self.payload)}],
        }
        self.inbox = queue.Queue()
        self.inbox.put(deepcopy(self.payload))
        self.calls = []

    def call_tagged(self, method, **params):
        self.calls.append((method, params))
        return {"task_id": "side_original"}, self.epoch

    def call(self, method, **params):
        assert method == "session.resume"
        assert params == {"session_id": "parent"}
        self.calls.append((method, params))
        return deepcopy(self.snapshot)

    def side_waiter(self, _task_id):
        return self.inbox

    def drop_side_waiter(self, _task_id):
        pass

    def get_meta(self, _key):
        return json.dumps({"status": "completed", "task_id": self.payload["task_id"],
                           "profile_id": self.snapshot["info"]["profile_id"], "parent_session_id": "parent",
                           "event_type": f"{self.verb}.complete", "payload": self.payload})


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shown = []
    cli = SimpleNamespace(session_id="parent", _app=None, conversation_history=[{"role": "user", "content": "original"}])
    monkeypatch.setattr(commands, "_print_side_result_panel", lambda _cli, **kw: shown.append(kw["body"]))
    monkeypatch.setattr(commands, "_refresh_tui_before_print", lambda _cli: None)
    monkeypatch.setattr("hermes_cli.cli_commands_session_tools._cp", lambda text: shown.append(text))
    return cli, shown


def start_worker(cli, connection):
    cli._session_db = GatewaySessionStore(connection)
    cli._session_db._reader = connection
    _, wait = gateway_side.start(connection, "parent", connection.verb, "inspect")
    thread = side.side_worker(cli, wait, name="side-result-test", fail_label="side",
                              header_lines=[], title_suffix="", empty_note="")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return wait


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_sealed_failure_live_then_resume_shows_once(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: original provider refusal")
    before = deepcopy(cli.conversation_history)
    start_worker(cli, connection)
    side.display_results(cli)
    assert len(shown) == 1
    assert "original provider refusal" in shown[0]
    assert cli.conversation_history == before


@pytest.mark.parametrize("verb", ["background", "btw"])
@pytest.mark.parametrize("foreign", ["session", "profile"])
def test_failure_waiter_cannot_consume_in_a_foreign_owner(viewer, tmp_path, monkeypatch, verb, foreign):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: original provider refusal")
    original = connection.call_tagged

    def admit(*args, **kwargs):
        result = original(*args, **kwargs)
        if foreign == "session":
            cli.session_id = "other"
        else:
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
        return result

    connection.call_tagged = admit
    start_worker(cli, connection)
    assert shown == []
    assert cli._background_results_shown == set()
    cli.session_id = "parent"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    side.display_results(cli)
    side.display_results(cli)
    assert len(shown) == 1
    assert "original provider refusal" in shown[0]


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_cold_snapshot_wins_a_late_failure_waiter(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: original provider refusal")
    cli._session_db = GatewaySessionStore(connection)
    cli._session_db._reader = connection
    side.display_results(cli)
    assert shown == ["error: original provider refusal"]
    start_worker(cli, connection)
    assert shown == ["error: original provider refusal"]


@pytest.mark.parametrize("verb", ["background", "btw"])
@pytest.mark.parametrize("foreign", ["session", "profile", None])
def test_unconfirmed_body_is_retained_without_seen_or_foreign_output(viewer, tmp_path, monkeypatch, verb, foreign):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: result_not_durable; preserve before restart.\nORIGINAL_PRIVATE_RESULT")
    connection.snapshot["background_results"] = []
    original = connection.call_tagged

    def admit(*args, **kwargs):
        result = original(*args, **kwargs)
        if foreign == "session":
            cli.session_id = "other"
        elif foreign == "profile":
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
        return result

    connection.call_tagged = admit
    start_worker(cli, connection)
    assert len(shown) == (0 if foreign else 1)
    assert cli._background_results_shown == set()
    assert len(cli._background_results_pending) == 1
    cli.session_id = "parent"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    side.display_results(cli)
    side.display_results(cli)
    assert len(shown) == 1
    assert "ORIGINAL_PRIVATE_RESULT" in shown[0]
    assert cli._background_results_shown == set()
    assert "ORIGINAL_PRIVATE_RESULT" in next(iter(cli._background_results_pending.values()))[0].text


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_durable_error_named_result_not_durable_is_still_canonical(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: result_not_durable is ordinary business text")
    start_worker(cli, connection)
    side.display_results(cli)
    assert shown == [connection.payload["text"]]
    assert len(cli._background_results_shown) == 1
    assert cli._background_results_pending == {}


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_disconnect_retains_actionable_error_without_consuming_a_result(viewer, tmp_path, monkeypatch, verb):
    from hermes_cli.gateway_connection import DISCONNECTED
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "unused")
    connection.inbox.get_nowait()
    connection.closed = True
    connection.snapshot["background_results"] = []
    monkeypatch.setattr(gateway_side, "_POLL_SECONDS", 0.001)
    start_worker(cli, connection)
    assert len(shown) == 1
    assert DISCONNECTED in shown[0]
    assert cli._background_results_shown == set()


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_snapshot_outage_does_not_drop_in_memory_error(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: ORIGINAL_PRIVATE_RESULT")

    def unavailable(*args, **kwargs):
        raise RuntimeError("gateway disconnected")

    connection.call = unavailable
    start_worker(cli, connection)
    assert any("ORIGINAL_PRIVATE_RESULT" in line for line in shown)
    assert cli._background_results_shown == set()
    assert len(cli._background_results_pending) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_in_memory_result_does_not_consume_a_different_canonical_body(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: result_not_durable\nORIGINAL_PRIVATE_RESULT")
    connection.snapshot["background_results"][0]["payload"]["text"] = "error: unknown_execution"
    start_worker(cli, connection)
    assert cli._background_results_shown == set()
    assert len(shown) == 1
    assert "ORIGINAL_PRIVATE_RESULT" in shown[0]
    assert len(cli._background_results_pending) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
@pytest.mark.parametrize("foreign", ["session", "profile"])
def test_switch_while_snapshot_is_read_cannot_print_old_parent(viewer, tmp_path, monkeypatch, verb, foreign):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: ORIGINAL_PRIVATE_RESULT")
    original = connection.call

    def call(*args, **kwargs):
        result = original(*args, **kwargs)
        if foreign == "session":
            cli.session_id = "other"
        else:
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
        return result

    connection.call = call
    start_worker(cli, connection)
    assert shown == []
    assert cli._background_results_shown == set()


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_print_failure_retains_result_until_later_resume(viewer, tmp_path, monkeypatch, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: ORIGINAL_PRIVATE_RESULT")

    def broken(*args, **kwargs):
        raise RuntimeError("display unavailable")

    monkeypatch.setattr(commands, "_print_side_result_panel", broken)
    start_worker(cli, connection)
    assert cli._background_results_shown == set()
    assert len(cli._background_results_pending) == 1
    monkeypatch.setattr(commands, "_print_side_result_panel", lambda _cli, **kw: shown.append(kw["body"]))
    side.display_results(cli)
    assert shown[-1] == connection.payload["text"]
    assert len(cli._background_results_shown) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_cold_unknown_notice_is_not_a_durable_seen_ack(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: unknown_execution; reconcile")
    connection.get_meta = lambda _key: json.dumps({"status": "unknown"})
    cli._session_db = GatewaySessionStore(connection)
    cli._session_db._reader = connection
    side.display_results(cli)
    side.display_results(cli)
    assert shown == [connection.payload["text"]]
    assert cli._background_results_shown == set()
    # A later sealed completion must remain displayable for the same task.
    connection.payload["text"] = "sealed late completion"
    connection.snapshot["background_results"][0]["payload"] = deepcopy(connection.payload)
    connection.get_meta = lambda key: Connection.get_meta(connection, key)
    side.display_results(cli)
    assert shown == ["error: unknown_execution; reconcile", "sealed late completion"]
    assert len(cli._background_results_shown) == 1


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_switch_during_metadata_read_does_not_print_original_body(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: ORIGINAL_PRIVATE_RESULT")
    original = connection.get_meta

    def switch(key):
        value = original(key)
        cli.session_id = "other"
        return value

    connection.get_meta = switch
    start_worker(cli, connection)
    assert shown == []
    assert cli._background_results_shown == set()


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_success_is_canonical_once_and_preserves_completion_bell(viewer, tmp_path, verb):
    from unittest.mock import MagicMock
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "sealed success")
    cli._session_db = GatewaySessionStore(connection)
    cli._session_db._reader = connection
    cli._ring_bell = MagicMock()
    _, wait = gateway_side.start(connection, "parent", verb, "inspect")
    thread = side.side_worker(cli, wait, name="success-result-test", fail_label="side",
                              header_lines=[], title_suffix="", empty_note="", bell=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    side.display_results(cli)
    assert shown == ["sealed success"]
    cli._ring_bell.assert_called_once()


@pytest.mark.parametrize("verb", ["background", "btw"])
def test_replaced_profile_store_blocks_worker_with_old_context(viewer, tmp_path, verb):
    cli, shown = viewer
    connection = Connection(tmp_path, verb, "error: ORIGINAL_PRIVATE_RESULT")
    original = connection.call_tagged

    def replace_store(*args, **kwargs):
        answer = original(*args, **kwargs)
        cli._session_db.current = {"stored_session_id": "parent", "info": {"profile_id": str(tmp_path / "other")}}
        return answer

    connection.call_tagged = replace_store
    start_worker(cli, connection)
    assert shown == []
    assert cli._background_results_shown == set()
    cli._session_db.current = {"stored_session_id": "parent", "info": {"profile_id": str(tmp_path)}}
    side.display_results(cli)
    assert shown == [connection.payload["text"]]
