import json
import os
from functools import partial

import psutil
import pytest

from agent.runtime_session_store import RuntimeSessionStore
from gateway.session_worker_child import reserve_child_execution
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
import hermes_state_runtime as runtime
from tests.hermes_state import test_tool_permission_recovery_r2 as fixtures


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n  streaming: false\n")
    with SessionDB(tmp_path / "state.db") as db:
        yield db


@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("allowed", [[], ["read_file"]])
def test_missing_row_restricted_pin_cannot_succeed(state, preserve, allowed):
    before = state._read_one("SELECT COUNT(*) FROM system_prompts")[0]
    with pytest.raises(ValueError, match="missing|exist|found"):
        state.update_session_tool_names("absent", fixtures._pin(allowed), preserve_cache=preserve)
    assert state.get_session("absent") is None
    assert state._read_one("SELECT COUNT(*) FROM system_prompts")[0] == before


@pytest.mark.parametrize("allowed", [[], ["read_file"], None])
def test_fresh_first_turn_persists_grant_for_second_receiver(state, tmp_path, allowed):
    first = fixtures._agent(state, session_id="fresh", allowed_tools=allowed)
    fixtures._unit_responses(first)
    second = None
    try:
        assert state.get_session("fresh") is None
        result = first.run_conversation("Respond without tools")
        assert result["completed"], result
        row = state.get_session("fresh")
        assert row is not None
        meta = json.loads(row["model_config"])["_tool_permissions"]
        assert meta == {"version": 1, "allowed_tools": allowed}
        second = fixtures._agent(state, session_id="fresh")
        fixtures._unit_responses(second)
        assert second.run_conversation("Continue without tools")["completed"]
        assert second.allowed_tools == allowed
        if allowed is not None:
            assert second.valid_tool_names == set(allowed)
            assert "not permitted" in second._invoke_tool("terminal", {"command": "false"}, "denied")
    finally:
        if second:
            second.close()
        first.close()


@pytest.mark.parametrize("allowed", [[], ["read_file"], None])
@pytest.mark.parametrize("lineage", [None, {"_branched_from": "root"}])
def test_public_child_and_fork_inherit_grant_across_generations(state, allowed, lineage):
    from agent.conversation_loop import _restore_pinned_tools
    state.create_session("root", "cli")
    if allowed is not None:
        state.update_session_tool_names("root", fixtures._pin(allowed))
    state.create_session("child", "cli", parent_session_id="root", model_config=lineage)
    state.create_session("grandchild", "cli", parent_session_id="child")
    for sid in ["child", "grandchild"]:
        receiver = fixtures._agent(state, session_id=sid)
        try:
            _restore_pinned_tools(receiver, state.get_session(sid))
            assert receiver.allowed_tools == allowed
            if allowed is not None:
                assert receiver.valid_tool_names == set(allowed)
                assert "not permitted" in receiver._invoke_tool("terminal", {"command": "false"}, "denied")
        finally:
            receiver.close()
    if lineage:
        assert json.loads(state.get_session("child")["model_config"])["_branched_from"] == "root"


def test_omitted_child_config_preserves_narrower_grant_and_blocks_widening(state):
    state.create_session("root", "cli")
    state.update_session_tool_names("root", fixtures._pin(["read_file", "terminal"]))
    state.create_session("child", "cli", parent_session_id="root")
    state.update_session_tool_names("child", fixtures._pin(["read_file"]))
    state.create_session("child", "cli", parent_session_id="root", model_config={"max_iterations": 3})
    row = state.get_session("child")
    assert json.loads(row["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]
    with pytest.raises(ValueError, match="expand|parent|reserved|existing"):
        state.create_session("child", "cli", parent_session_id="root", model_config={
            "_tool_permissions": {"version": 1, "allowed_tools": ["read_file", "terminal"]}})
    assert state.get_session("child")["model_config"] == row["model_config"]


@pytest.mark.parametrize("allowed", [[], ["read_file"], None])
def test_managed_child_complete_first_turn_persists_constructor_and_owner_lineage(state, tmp_path, monkeypatch, allowed):
    from tools import delegate_tool as delegation
    monkeypatch.setattr(delegation, "_load_config", dict)
    state.create_session("parent", "cli", profile_name="fixture")
    epoch = runtime.begin_runtime_epoch(state, instance_id="managed-permission")
    scope = dict(epoch=epoch, execution_id="worker", session_id="parent", generation=0)
    runtime.register_worker_execution(state, **scope, kind="compute", adoption_secret="fixture-secret")
    operations = []

    def rpc(method, **params):
        values = {key: params[key] for key in ("epoch", "execution_id", "session_id", "generation", "sequence", "operation", "payload")}
        operations.append((values["session_id"], values["operation"]))
        if values["operation"] == "child.reserve":
            handler = partial(reserve_child_execution, epoch=epoch, execution_id=values["execution_id"],
                              identity={"profile_id": "fixture", "pid": os.getpid(), "birth": worker_birth(psutil.Process())},
                              principal="local", limits=(2, 2), refused=None)
            return runtime.apply_worker_receipt(state, **values, handlers={"child.reserve": handler})
        return runtime.mutate_worker_execution(state, **values)

    store = RuntimeSessionStore(rpc, scope, tmp_path / "parent-outbox")
    parent = fixtures._agent(store, session_id="parent")
    child = None
    try:
        child = delegation._build_child_agent(task_index=0, goal="Respond", context=None, toolsets=None, model=None,
                                              max_iterations=4, task_count=1, parent_agent=parent, allowed_tools=allowed)
        expected = dict(child._session_init_model_config)
        fixtures._unit_responses(child)
        result = child.run_conversation("Respond without tools")
        assert result["completed"], result
        row = state.get_session(child.session_id)
        config = json.loads(row["model_config"])
        assert child._session_db_created is True
        assert row["model"] == child.model
        for key in ["max_iterations", "max_tokens", "reasoning_config"]:
            assert key in config
            assert config[key] == expected[key]
        assert config["_delegate_from"] == "parent"
        assert row["parent_session_id"] == "parent"
        assert row["profile_name"] == "fixture"
        assert config["_tool_permissions"] == {"version": 1, "allowed_tools": allowed}
        assert (child.session_id, "session.create") in operations
    finally:
        if child:
            child.close()
        parent.close()
        store.close()
