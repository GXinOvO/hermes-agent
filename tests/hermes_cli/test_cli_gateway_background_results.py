"""The classic gateway-backed viewer restores the owner's sealed side channel."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import cli_background_results as side, cli_commands_mixin as commands
from hermes_cli.gateway_session_store import GatewaySessionStore


def _viewer(tmp_path, monkeypatch, *, stored="parent", profile=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    events = [{"event_type": "background.complete", "payload": {"task_id": "bg_real", "text": "sealed answer"}}]
    snapshot = {"session_id": stored, "stored_session_id": stored,
                "info": {"profile_id": profile or str(tmp_path)}, "background_results": events}
    connection = SimpleNamespace(final=False, call=MagicMock(return_value=snapshot),
                                 state=lambda _sid: SimpleNamespace(adopt_snapshot=lambda _s: None))
    store = GatewaySessionStore(connection)
    def metadata(key):
        for event in snapshot["background_results"]:
            task_id = event["payload"]["task_id"]
            if key == "background-result:" + hashlib.sha256(task_id.encode()).hexdigest():
                return json.dumps({"status": "completed", "task_id": task_id,
                                   "profile_id": snapshot["info"]["profile_id"], "parent_session_id": stored,
                                   "event_type": event["event_type"], "payload": event["payload"]})
        return None
    store._reader = SimpleNamespace(get_meta=metadata)
    viewer = SimpleNamespace(_session_db=store, session_id="parent", conversation_history=[{"role": "user", "content": "original"}])
    panels = []
    monkeypatch.setattr(commands, "_print_side_result_panel", lambda _cli, **kw: panels.append(kw))
    return viewer, snapshot, connection, panels


def test_canonical_side_results_restore_once_without_mutating_model_history(tmp_path, monkeypatch):
    viewer, snapshot, connection, panels = _viewer(tmp_path, monkeypatch)
    before = deepcopy(viewer.conversation_history)
    viewer._session_db.snapshots["parent"] = snapshot
    side.display_results(viewer)
    connection.call.assert_not_called()
    side.display_results(viewer)
    assert [p["body"] for p in panels] == ["sealed answer"]
    assert viewer.conversation_history == before
    assert connection.call.call_args.kwargs == {"session_id": "parent"}
    assert connection.call.call_args.args == ("session.resume",)
    # An already-attached store must refresh the side channel: cached attach predates this result.
    snapshot["background_results"].append({"event_type": "btw.complete", "payload": {"task_id": "btw_later", "text": "later"}})
    side.display_results(viewer)
    assert [p["body"] for p in panels] == ["sealed answer", "later"]


@pytest.mark.parametrize("foreign", ["session", "profile"])
def test_canonical_side_results_refuse_foreign_binding(tmp_path, monkeypatch, foreign):
    viewer, _snapshot, _connection, panels = _viewer(
        tmp_path, monkeypatch, stored="elsewhere" if foreign == "session" else "parent",
        profile=str(tmp_path / "elsewhere") if foreign == "profile" else None)
    monkeypatch.setattr("hermes_cli.cli_commands_session_tools._cp", lambda *_a: None)
    side.display_results(viewer)
    assert panels == []
