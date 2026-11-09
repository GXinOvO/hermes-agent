"""Durable permission recovery and reserved-row regressions from Sol review r2.

Inference is a UNIT response double; SessionDB, AIAgent's complete turn loop and
native file dispatch are real. All state lives under the runner's private home.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _pin(allowed):
    return {"version": "recovery-r2", "tools": [], "allowed_tools": allowed}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n  streaming: false\n")
    with SessionDB(tmp_path / "state.db") as db:
        yield db


def _agent(db, **kwargs):
    return AIAgent(api_key="unit-no-network", base_url="http://127.0.0.1:9/v1",
                   provider="custom", api_mode="chat_completions", model="unit-allowlist",
                   platform="cli", quiet_mode=True, skip_context_files=True,
                   skip_memory=True, skip_background_review=True,
                   enabled_toolsets=["file", "terminal"], max_iterations=4,
                   session_db=db, **kwargs)


def _unit_responses(agent, path=None):
    agent._build_system_prompt = lambda msg=None: "Recovery r2 prompt"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent._disable_streaming = True
    messages = []
    if path is not None:
        call = SimpleNamespace(id="recovery-read", type="function", function=SimpleNamespace(
            name="read_file", arguments=json.dumps({"path": str(path)})))
        messages.append(SimpleNamespace(content=None, tool_calls=[call]))
    messages.append(SimpleNamespace(content="Recovery complete", tool_calls=None))
    responses = iter(SimpleNamespace(choices=[SimpleNamespace(message=msg,
        finish_reason="tool_calls" if msg.tool_calls else "stop")], model="unit-allowlist", usage=None)
        for msg in messages)
    client = MagicMock()
    client.chat.completions.create.side_effect = lambda **kwargs: next(responses)
    agent.client = client
    return client


@pytest.mark.parametrize("authority", ["restricted", "legacy", "trusted-unrestricted"])
def test_historyless_loop_restores_durable_authority(state, tmp_path, authority):
    state.create_session("owned", "cli")
    if authority == "restricted":
        state.update_session_tool_names("owned", _pin([]))
    elif authority == "trusted-unrestricted":
        state.update_session_tool_names("owned", {"version": "legacy", "tools": []})
    marker = tmp_path / "native-marker.txt"
    marker.write_text("RECOVERY-R2-NATIVE-READ")
    agent = _agent(state, session_id="owned")  # constructor allowed_tools deliberately omitted
    client = _unit_responses(agent, marker)
    try:
        result = agent.run_conversation("Respond to the request", conversation_history=None)
        assert result["completed"], result
        outputs = [m for m in result["messages"] if m.get("role") == "tool"]
        assert outputs, "complete loop must consume the requested native tool call"
        names = {t["function"]["name"] for t in client.chat.completions.create.call_args_list[0].kwargs.get("tools", [])}
        if authority == "restricted":
            assert agent.allowed_tools == []
            assert not names
            assert "RECOVERY-R2-NATIVE-READ" not in str(outputs)
            assert "RECOVERY-R2-NATIVE-READ" not in str(agent._invoke_tool("read_file", {"path": str(marker)}, "probe"))
            assert json.loads(state.get_session("owned")["model_config"])["_tool_permissions"]["allowed_tools"] == []
        else:
            assert agent.allowed_tools is None
            assert "read_file" in names
            assert "RECOVERY-R2-NATIVE-READ" in str(outputs)
    finally:
        agent.close()


def test_authority_read_failure_leaves_no_tools_before_loop_dispatch(state, tmp_path, monkeypatch):
    state.create_session("owned", "cli")
    state.update_session_tool_names("owned", _pin([]))
    readback = state.get_session
    before = readback("owned")["model_config"]
    marker = tmp_path / "native-marker.txt"
    marker.write_text("RECOVERY-R2-READ-FAIL-NATIVE")
    agent = _agent(state, session_id="owned")
    client = _unit_responses(agent, marker)

    def fail_read(sid):
        raise RuntimeError("recovery-authority-read-failed")

    monkeypatch.setattr(state, "get_session", fail_read)
    try:
        from agent.conversation_loop import _restore_or_build_system_prompt
        try:
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "Continue"}])
        except RuntimeError as exc:
            assert "authority" in str(exc)
        assert not client.chat.completions.create.called
        assert agent.allowed_tools == []
        assert not agent.tools and not agent.valid_tool_names
        assert "RECOVERY-R2-READ-FAIL-NATIVE" not in str(agent._invoke_tool("read_file", {"path": str(marker)}, "probe"))
        assert readback("owned")["model_config"] == before
    finally:
        agent.close()


@pytest.mark.parametrize("replacement", [None, {"version": 1, "allowed_tools": None},
    {"version": 1, "allowed_tools": ["read_file", "terminal"]}], ids=["remove", "unrestrict", "widen"])
def test_conversation_key_writer_refuses_permission_edits(state, tmp_path, replacement):
    from agent.conversation_loop import _restore_pinned_tools
    state.create_session("owned", "cli")
    state.update_session_tool_names("owned", _pin([]))
    state.update_session_tool_names("owned", None)
    before = tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'"))
    with pytest.raises(ValueError, match="reserved"):
        state.update_conversation_model_config_value("owned", "_tool_permissions", lambda old: replacement)
    assert tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'")) == before
    receiver = _agent(state, session_id="owned")
    marker = tmp_path / "refused-read.txt"
    marker.write_text("RECOVERY-R2-WRITER-NATIVE")
    try:
        _restore_pinned_tools(receiver, state.get_session("owned"))
        assert receiver.allowed_tools == []
        assert "RECOVERY-R2-WRITER-NATIVE" not in str(receiver._invoke_tool("read_file", {"path": str(marker)}, "probe"))
    finally:
        receiver.close()


def test_conversation_key_writer_keeps_unrelated_updates_and_exact_authority(state):
    state.create_session("owned", "cli")
    state.update_session_tool_names("owned", _pin([]))
    # The callback may return the existing sidecar unchanged, just like patch/replace.
    state.update_conversation_model_config_value("owned", "_tool_permissions", lambda old: old)
    assert state.update_conversation_model_config_value("owned", "_unrelated", lambda old: {"key": 3}) == "owned"
    config = json.loads(state.get_session("owned")["model_config"])
    assert config["_tool_permissions"] == {"version": 1, "allowed_tools": []}
    assert config["_unrelated"] == {"key": 3}


def test_conversation_key_writer_cannot_create_reserved_sidecar(state):
    state.create_session("owned", "cli")
    before = tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'"))
    with pytest.raises(ValueError, match="reserved|permission"):
        state.update_conversation_model_config_value("owned", "_tool_permissions", lambda old: None)
    assert tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'")) == before


@pytest.mark.parametrize("raw", [None, "unreadable-cache", '{"version":"old","tools":null}',
    "null", "f" * 64], ids=["absent-control", "unreadable", "invalid-tools", "null", "swept-hash"])
def test_corrupt_cache_preserves_bytes_while_committing_narrower_authority(state, tmp_path, raw):
    from agent.conversation_loop import _restore_pinned_tools
    from tools.mcp_tool_agent import persist_agent_tool_names
    state.create_session("owned", "cli")
    state.update_session_tool_names("owned", _pin(["read_file", "terminal"]))
    state._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (raw,))
    first = _agent(state, session_id="owned", allowed_tools=["read_file"])
    second = None
    try:
        _restore_pinned_tools(first, state.get_session("owned"))
        assert first.allowed_tools == ["read_file"]
        persist_agent_tool_names(first)
        stored = state.get_session("owned")
        if raw is not None:
            assert tuple(state._read_one("SELECT tool_names FROM sessions WHERE id='owned'")) == (raw,)
        assert json.loads(stored["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]
        second = _agent(state, session_id="owned")
        _restore_pinned_tools(second, stored)
        assert second.allowed_tools == ["read_file"]
        assert second.valid_tool_names == {"read_file"}
        assert "not permitted" in second._invoke_tool("terminal", {"command": "printf RECOVERY-R2-FORBIDDEN"}, "probe").lower()
        marker = tmp_path / "allowed-read.txt"
        marker.write_text("RECOVERY-R2-ALLOWED-READ")
        assert "RECOVERY-R2-ALLOWED-READ" in second._invoke_tool("read_file", {"path": str(marker)}, "probe")
    finally:
        first.close()
        if second:
            second.close()


def test_corrupt_cache_permission_write_failure_propagates(state):
    import sqlite3
    from agent.conversation_loop import _restore_pinned_tools
    from tools.mcp_tool_agent import persist_agent_tool_names
    state.create_session("owned", "cli")
    state.update_session_tool_names("owned", _pin(["read_file", "terminal"]))
    state._write_sql("UPDATE sessions SET tool_names='unreadable-cache' WHERE id='owned'")
    before = tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'"))
    first = _agent(state, session_id="owned", allowed_tools=["read_file"])
    try:
        _restore_pinned_tools(first, state.get_session("owned"))
        state._write_sql("CREATE TRIGGER reject_permission BEFORE UPDATE OF model_config ON sessions "
                         "BEGIN SELECT RAISE(ABORT, 'permission-write-failed'); END")
        with pytest.raises(sqlite3.IntegrityError, match="permission-write-failed"):
            persist_agent_tool_names(first)
        assert tuple(state._read_one("SELECT tool_names,model_config FROM sessions WHERE id='owned'")) == before
    finally:
        first.close()



def test_child_upsert_intersects_existing_narrower_permission_and_merges_constructor_fields(state):
    state.create_session("parent", "cli")
    state.update_session_tool_names("parent", _pin(["read_file", "terminal"]))
    state.create_session("owned", "subagent", parent_session_id="parent")
    state.update_session_tool_names("owned", _pin(["read_file"]))
    state.create_session("owned", "subagent", parent_session_id="parent",
                        model_config={"max_iterations": 4, "max_tokens": 321})
    row = state.get_session("owned")
    config = json.loads(row["model_config"])
    assert config["_tool_permissions"]["allowed_tools"] == ["read_file"]
    assert config["max_iterations"] == 4
    assert config["max_tokens"] == 321


@pytest.mark.parametrize("parent_id", [None, "parent"])
def test_constructor_backfills_missing_config_without_overwriting_existing_values(state, parent_id):
    if parent_id:
        state.create_session(parent_id, "cli")
        state.update_session_tool_names(parent_id, _pin(["read_file", "terminal"]))
    state.create_session("owned", "subagent", parent_session_id=parent_id)
    state.update_session_tool_names("owned", _pin(["read_file"]))
    state.create_session("owned", "subagent", parent_session_id=parent_id,
                         model_config={"max_iterations": 5, "max_tokens": None})
    state.create_session("owned", "subagent", parent_session_id=parent_id,
                         model_config={"max_iterations": 10, "max_tokens": 321,
                                       "reasoning_config": {"enabled": True, "effort": "max"}})
    config = json.loads(state.get_session("owned")["model_config"])
    assert config["max_iterations"] == 5
    assert config["max_tokens"] is None
    assert config["reasoning_config"] == {"enabled": True, "effort": "max"}
    assert config["_tool_permissions"]["allowed_tools"] == ["read_file"]


def test_child_upsert_rejects_permission_widening_and_rolls_back_constructor_update(state):
    state.create_session("parent", "cli")
    state.update_session_tool_names("parent", _pin(["read_file"]))
    state.create_session("owned", "subagent", parent_session_id="parent",
                        model_config={"max_iterations": 2})
    before = state.get_session("owned")["model_config"]
    with pytest.raises(ValueError, match="reserved|expand|widen"):
        state.create_session("owned", "subagent", parent_session_id="parent",
                            model_config={"_tool_permissions": {"version": 1,
                                "allowed_tools": ["read_file", "terminal"]}, "max_iterations": 9})
    after = state.get_session("owned")
    assert after["model_config"] == before


def test_root_constructor_cannot_create_reserved_permission_sidecar(state):
    with pytest.raises(ValueError, match="reserved|permission"):
        state.create_session("root", "cli", model_config={"_tool_permissions": {
            "version": 1, "allowed_tools": ["read_file"]}})


def test_child_constructor_narrows_legacy_parent_pin_before_sidecar_migration(state):
    state.create_session("parent", "cli")
    state._write_sql("UPDATE sessions SET tool_names=? WHERE id='parent'", (
        json.dumps(_pin(["read_file"])),))
    with pytest.raises(ValueError, match="expand|parent|reserved"):
        state.create_session("owned", "subagent", parent_session_id="parent",
                            model_config={"_tool_permissions": {"version": 1,
                                "allowed_tools": ["terminal"]}})


def test_agent_constructor_permission_mismatch_is_not_retryable(state):
    state.create_session("parent", "cli")
    state.update_session_tool_names("parent", _pin(["read_file", "terminal"]))
    state.create_session("owned", "subagent", parent_session_id="parent")
    state.update_session_tool_names("owned", _pin(["read_file"]))
    agent = _agent(state, session_id="owned", parent_session_id="parent")
    agent._session_init_model_config["_tool_permissions"] = {
        "version": 1, "allowed_tools": ["read_file", "terminal"]}
    try:
        with pytest.raises(ValueError, match="expand|reserved|permission"):
            agent._ensure_db_session()
        assert agent._session_db_created is False
    finally:
        agent.close()


@pytest.mark.parametrize("writer", ["session", "worker"])
def test_corrupt_cache_narrowing_uses_native_transaction_on_either_store(state, writer):
    from agent.conversation_loop import _restore_pinned_tools
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
    from tools.mcp_tool_agent import persist_agent_tool_names
    state.create_session("owned", "subagent")
    state.update_session_tool_names("owned", _pin(["read_file", "terminal"]))
    state._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", ("corrupt-cache",))
    store = None
    db = state
    if writer == "worker":
        epoch = begin_runtime_epoch(state, instance_id="permission-recovery")
        scope = dict(epoch=epoch, execution_id="permission-worker", session_id="owned", generation=0)
        register_worker_execution(state, **scope, kind="child", adoption_secret="fixture-secret")
        store = RuntimeSessionStore(lambda method, **params: mutate_worker_execution(state, **params),
                                    scope, state.db_path.parent / "permission-outbox")
        db = store
    agent = SimpleNamespace(allowed_tools=["read_file"],
                            tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
                            valid_tool_names={"read_file"}, _session_db=db, session_id="owned")
    try:
        _restore_pinned_tools(agent, db.get_session("owned"))
        persist_agent_tool_names(agent)
        row = state.get_session("owned")
        assert row["tool_names"] == "corrupt-cache"
        assert json.loads(row["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]
    finally:
        if store:
            store.close()


def test_reserved_child_row_preserves_restriction_before_first_turn(state, monkeypatch):
    from tools import delegate_tool as delegation
    from agent.conversation_loop import _restore_pinned_tools
    monkeypatch.setattr(delegation, "_load_config", dict)
    state.create_session("parent", "cli")
    parent = _agent(state, session_id="parent")
    child = delegation._build_child_agent(
        task_index=0, goal="read only", context=None, toolsets=None, model=None,
        max_iterations=3, task_count=1, parent_agent=parent, allowed_tools=["read_file"],
    )
    receiver = None
    try:
        row = child._session_db.get_session(child.session_id)
        receiver = _agent(child._session_db, session_id=child.session_id)
        _restore_pinned_tools(receiver, row)
        assert receiver.allowed_tools == ["read_file"]
        assert receiver.valid_tool_names == {"read_file"}
        assert "max_iterations" not in json.loads(row["model_config"])
    finally:
        if receiver:
            receiver.close()
        child.close()
        parent.close()


def test_build_child_first_turn_keeps_constructor_config_with_inherited_sidecar(state, monkeypatch):
    from tools import delegate_tool as delegation
    from tools.mcp_tool_agent import persist_agent_tool_names
    monkeypatch.setattr(delegation, "_load_config", dict)
    state.create_session("parent", "cli")
    state.update_session_tool_names("parent", _pin(["read_file", "terminal"]))
    parent = _agent(state, session_id="parent", allowed_tools=["read_file", "terminal"])
    child = delegation._build_child_agent(
        task_index=0, goal="read only", context=None, toolsets=None, model=None,
        max_iterations=4, task_count=1, parent_agent=parent, allowed_tools=["read_file"],
    )
    try:
        child._cached_system_prompt = "child constructor prompt"
        child._ensure_db_session()
        before_pin = json.loads(child._session_db.get_session(child.session_id)["model_config"])
        assert before_pin["max_iterations"] == 4
        assert before_pin["_tool_permissions"]["allowed_tools"] == ["read_file"]
        persist_agent_tool_names(child)
        row = child._session_db.get_session(child.session_id)
        config = json.loads(row["model_config"])
        assert config["max_iterations"] == 4
        assert "max_tokens" in config
        assert "reasoning_config" in config
        assert config["_tool_permissions"]["allowed_tools"] == ["read_file"]
    finally:
        child.close()
        parent.close()


def test_build_child_first_turn_legacy_parent_pins_explicit_child_after_constructor(state, monkeypatch):
    from tools import delegate_tool as delegation
    from tools.mcp_tool_agent import persist_agent_tool_names
    monkeypatch.setattr(delegation, "_load_config", dict)
    state.create_session("parent", "cli")
    state.update_session_tool_names("parent", {"version": "legacy", "tools": []})
    parent = _agent(state, session_id="parent")
    child = delegation._build_child_agent(
        task_index=0, goal="read only", context=None, toolsets=None, model=None,
        max_iterations=3, task_count=1, parent_agent=parent, allowed_tools=["read_file"],
    )
    try:
        child._cached_system_prompt = "legacy child prompt"
        child._ensure_db_session()
        before = json.loads(child._session_db.get_session(child.session_id)["model_config"])
        assert before["max_iterations"] == 3
        assert before["_tool_permissions"]["allowed_tools"] == ["read_file"]
        persist_agent_tool_names(child)
        after = json.loads(child._session_db.get_session(child.session_id)["model_config"])
        assert after["max_iterations"] == 3
        assert after["_tool_permissions"]["allowed_tools"] == ["read_file"]
    finally:
        child.close()
        parent.close()
