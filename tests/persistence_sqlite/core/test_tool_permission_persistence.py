"""Durable tool authorization at the native session/worker receiving boundary."""
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    RuntimeStoreError, begin_runtime_epoch, mutate_worker_execution, register_worker_execution,
)


def _pin(allowed):
    return {"version": "permission-test", "tools": [], "allowed_tools": allowed}


@pytest.fixture
def db(tmp_path):
    state = SessionDB(tmp_path / "state.db")
    state.create_session("owned", "subagent")
    state.create_session("foreign", "cli")
    yield state
    state.close()


def _scope(db):
    epoch = begin_runtime_epoch(db, instance_id="permission-test")
    scope = dict(epoch=epoch, execution_id="worker", session_id="owned", generation=0)
    register_worker_execution(db, **scope, kind="child", adoption_secret="fixture-secret")
    return scope


def _raw(db):
    return tuple(db._read_one("SELECT tool_names, model_config FROM sessions WHERE id='owned'"))


@pytest.mark.parametrize("writer", ["session", "worker"])
def test_receiving_writer_rejects_expansion_without_any_write(db, writer, record_property):
    db.update_session_tool_names("owned", _pin(["read_file"]))
    scope = _scope(db)
    before = _raw(db)
    receipts = db._read_one("SELECT COUNT(*) FROM worker_receipts")[0]
    with pytest.raises((ValueError, RuntimeStoreError), match="permission|widen|expand"):
        if writer == "session":
            db.update_session_tool_names("owned", _pin(["read_file", "terminal"]))
        else:
            mutate_worker_execution(db, **scope, sequence=1, operation="session.tools",
                                    payload={"tool_names": _pin(["read_file", "terminal"])})
    assert _raw(db) == before
    assert db._read_one("SELECT COUNT(*) FROM worker_receipts")[0] == receipts
    assert db._read_one("SELECT last_sequence FROM worker_executions")[0] == 0
    record_property("rejected_expansion_sql_readback", json.dumps({
        "writer": writer, "before": before, "after": _raw(db), "receipts": receipts,
        "last_sequence": db._read_one("SELECT last_sequence FROM worker_executions")[0]}))
    if writer == "worker":
        result = mutate_worker_execution(db, **scope, sequence=1, operation="session.tools",
                                         payload={"tool_names": _pin([])})
        assert result == {"value": None}
    else:
        db.update_session_tool_names("owned", _pin([]))
    assert json.loads(db.get_session("owned")["tool_names"])["allowed_tools"] == []


def _receiver(db):
    tools = [{"type": "function", "function": {"name": name, "parameters": {}}}
             for name in ["read_file", "terminal"]]
    return SimpleNamespace(tools=tools, valid_tool_names={"read_file", "terminal"},
                           enabled_toolsets=["file", "terminal"], disabled_toolsets=[],
                           allowed_tools=None, _session_db=db, session_id="owned")


@pytest.mark.parametrize("corrupt", ['{"version":"broken","allowed_tools": [', "f" * 64, "null"])
@pytest.mark.parametrize("allowed", [[], ["read_file"], "unknown"])
def test_restore_corrupt_pin_preserves_raw_and_never_grants_unknown_permission(db, corrupt, allowed):
    from agent.conversation_loop import _restore_pinned_tools
    if allowed != "unknown":
        db.update_session_tool_names("owned", _pin(allowed))
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (corrupt,))
    before = _raw(db)
    agent = _receiver(db)
    if allowed == "unknown":
        with pytest.raises(ValueError, match="permission|pin"):
            _restore_pinned_tools(agent, db.get_session("owned"))
        assert agent.allowed_tools == []
        assert agent.tools == []
    else:
        _restore_pinned_tools(agent, db.get_session("owned"))
        assert agent.allowed_tools == allowed
        assert agent.valid_tool_names == set(allowed)
    assert _raw(db) == before


@pytest.mark.parametrize("writer", ["session", "worker"])
@pytest.mark.parametrize("replacement", [None, ["read_file", "terminal"], {"version": "rebuilt", "tools": []}])
def test_cache_clear_or_legacy_rebuild_cannot_remove_authorization(db, writer, replacement):
    from agent.conversation_loop import _restore_pinned_tools
    db.update_session_tool_names("owned", _pin(["read_file"]))
    if writer == "worker":
        mutate_worker_execution(db, **_scope(db), sequence=1, operation="session.tools",
                                payload={"tool_names": replacement})
    else:
        db.update_session_tool_names("owned", replacement)
    row = db.get_session("owned")
    agent = _receiver(db)
    _restore_pinned_tools(agent, row)
    assert agent.allowed_tools == ["read_file"]
    assert agent.valid_tool_names == {"read_file"}
    assert json.loads(row["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]


def test_independent_permission_survives_compression_and_fresh_process(db, record_property):
    db.update_session_tool_names("owned", _pin(["read_file"]))
    db.update_session_tool_names("owned", None)
    assert db.try_acquire_compression_lock("owned", "permission-lock")
    db.publish_compression_child(parent_session_id="owned", child_session_id="tip", source="subagent",
                                 messages=[{"role": "user", "content": "continue"}],
                                 compression_lock_holder="permission-lock")
    assert json.loads(db.get_session("tip")["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]
    code = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.conversation_loop import _restore_pinned_tools
db = SessionDB(Path(sys.argv[1]))
tools = [{"type":"function", "function":{"name":n,"parameters":{}}} for n in ["read_file","terminal"]]
agent = SimpleNamespace(tools=tools,valid_tool_names={"read_file","terminal"},allowed_tools=None,
    enabled_toolsets=["file","terminal"],disabled_toolsets=[],_session_db=db,session_id="tip")
_restore_pinned_tools(agent, db.get_session("tip"))
print(json.dumps({"allowed":agent.allowed_tools,"names":sorted(agent.valid_tool_names),
    "stored":json.loads(db.get_session("tip")["model_config"])["_tool_permissions"]}))
db.close()
'''
    child = subprocess.run([sys.executable, "-B", "-c", code, str(db.db_path)],
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                           capture_output=True, text=True, check=False)
    assert child.returncode == 0, child.stderr
    actual = json.loads(child.stdout)
    record_property("fresh_process_readback", child.stdout.strip())
    assert actual == {"allowed": ["read_file"], "names": ["read_file"],
                      "stored": {"version": 1, "allowed_tools": ["read_file"]}}


def test_restricted_persist_failure_is_not_silently_ignored(db):
    from tools.mcp_tool_agent import persist_agent_tool_names
    agent = _receiver(db)
    agent.allowed_tools = []
    db._write_sql("CREATE TRIGGER reject_pin BEFORE UPDATE OF tool_names ON sessions "
                  "BEGIN SELECT RAISE(ABORT, 'pin-write-failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="pin-write-failed"):
        persist_agent_tool_names(agent)
    assert _raw(db) == (None, None)


@pytest.mark.parametrize("operation", ["replace", "patch"])
def test_model_config_writers_preserve_reserved_permission(db, operation):
    db.update_session_tool_names("owned", _pin([]))
    if operation == "replace":
        db.update_session_meta("owned", json.dumps({"model": "new"}))
    else:
        with pytest.raises(ValueError, match="reserved|permission"):
            db.patch_session_model_config("owned", {"_tool_permissions": None})
    assert json.loads(_raw(db)[1])["_tool_permissions"] == {"version": 1, "allowed_tools": []}


def test_restore_does_not_ignore_restricted_pin_write_failure(db):
    from agent.conversation_loop import _restore_pinned_tools
    agent = _receiver(db)
    agent.allowed_tools = []
    db._write_sql("CREATE TRIGGER reject_pin BEFORE UPDATE OF tool_names ON sessions "
                  "BEGIN SELECT RAISE(ABORT, 'restore-write-failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="restore-write-failed"):
        _restore_pinned_tools(agent, db.get_session("owned"))
    assert _raw(db) == (None, None)


@pytest.mark.parametrize("tools", [None, "broken", [{"type": "function", "function": {}}]])
def test_restricted_invalid_tools_cache_keeps_raw_even_after_persist(db, tools):
    from agent.conversation_loop import _restore_pinned_tools
    from tools.mcp_tool_agent import persist_agent_tool_names
    db.update_session_tool_names("owned", _pin(["read_file"]))
    corrupt = json.dumps({"version": "broken", "tools": tools, "allowed_tools": ["read_file"]})
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (corrupt,))
    before = _raw(db)
    agent = _receiver(db)
    _restore_pinned_tools(agent, db.get_session("owned"))
    persist_agent_tool_names(agent)
    assert agent.valid_tool_names == {"read_file"}
    assert _raw(db) == before


@pytest.mark.parametrize("raw", [None, json.dumps(["read_file"]), json.dumps({"version": "legacy", "tools": []})])
def test_legacy_unrestricted_session_can_pin_and_rebuild(db, raw):
    from agent.conversation_loop import _restore_pinned_tools
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (raw,))
    agent = _receiver(db)
    _restore_pinned_tools(agent, db.get_session("owned"))
    assert agent.allowed_tools is None
    assert agent.valid_tool_names == {"read_file", "terminal"}
    db.update_session_tool_names("owned", _pin(["read_file", "terminal"]))


def test_trusted_unrestricted_marker_allows_swept_pin_rebuild(db):
    from agent.conversation_loop import _restore_pinned_tools
    db.update_session_tool_names("owned", {"version": "legacy", "tools": []})
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", ("f" * 64,))
    agent = _receiver(db)
    _restore_pinned_tools(agent, db.get_session("owned"))
    assert agent.allowed_tools is None
    assert json.loads(db.get_session("owned")["tool_names"])["tools"]


def test_worker_cannot_patch_reserved_key_or_foreign_session(db):
    db.update_session_tool_names("owned", _pin([]))
    scope = _scope(db)
    before = _raw(db)
    for operation, payload in [("session.meta", {"model_config_json": {"_tool_permissions": None}}),
                               ("session.publish_child", {"new_session_id": "tip", "source": "subagent",
                                                         "parent_session_id": "owned", "model_config": {"_tool_permissions": None}})]:
        with pytest.raises(RuntimeStoreError):
            mutate_worker_execution(db, **scope, sequence=1, operation=operation, payload=payload)
        assert _raw(db) == before
    foreign = {**scope, "session_id": "foreign"}
    with pytest.raises(RuntimeStoreError):
        mutate_worker_execution(db, **foreign, sequence=1, operation="session.tools", payload={"tool_names": _pin(["terminal"])})
    assert db.get_session("foreign")["tool_names"] is None


@pytest.mark.parametrize("raw", ["null", "{}", '[7]', '{"version":"bad","tools":null}'])
def test_unknown_invalid_pin_cannot_become_unrestricted(db, raw):
    from agent.conversation_loop import _restore_pinned_tools
    from tools.mcp_tool_agent import persist_agent_tool_names
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (raw,))
    agent = _receiver(db)
    with pytest.raises(ValueError, match="permission|pin"):
        _restore_pinned_tools(agent, db.get_session("owned"))
    persist_agent_tool_names(agent)
    assert agent.tools == []
    assert _raw(db) == (raw, None)


def test_worker_compression_publication_and_replay_keep_limit(db, record_property):
    from agent.conversation_loop import _restore_pinned_tools
    db.update_session_tool_names("owned", _pin(["read_file"]))
    db.update_session_tool_names("owned", None)
    scope = _scope(db)
    lock = mutate_worker_execution(db, **scope, sequence=1, operation="compression.lock.acquire",
                                   payload={"holder": "worker-compress"})
    assert lock["value"]
    payload = {"child_session_id": "worker-tip", "source": "subagent", "model": None,
               "model_config": {"model": "fixture"}, "system_prompt": None,
               "messages": [{"role": "user", "content": "continue"}],
               "compression_lock_holder": "worker-compress", "cwd": None, "profile_name": None,
               "require_compression_lease": True, "require_lease_refresh": True,
               "lease_ttl_seconds": 300.0, "watermark": None, "watermark_ceiling": None}
    result = mutate_worker_execution(db, **scope, sequence=2, operation="compression.publish", payload=payload)
    assert mutate_worker_execution(db, **scope, sequence=2, operation="compression.publish", payload=payload) == result
    row = db.get_session("worker-tip")
    record_property("worker_compression_sql_readback", json.dumps(dict(db._read_one(
        "SELECT id, parent_session_id, tool_names, model_config FROM sessions WHERE id='worker-tip'"))))
    assert json.loads(row["model_config"])["_tool_permissions"]["allowed_tools"] == ["read_file"]
    agent = _receiver(db)
    agent.session_id = "worker-tip"
    _restore_pinned_tools(agent, row)
    assert agent.valid_tool_names == {"read_file"}


@pytest.mark.parametrize("writer", ["session", "worker"])
def test_pre_sidecar_restricted_pin_migrates_without_unlocking(db, writer):
    from agent.conversation_loop import _restore_pinned_tools
    legacy = json.dumps(_pin(["read_file"]))
    db._write_sql("UPDATE sessions SET tool_names=? WHERE id='owned'", (legacy,))
    if writer == "session":
        with pytest.raises(ValueError, match="permission"):
            db.update_session_tool_names("owned", _pin(["terminal"]))
        assert _raw(db) == (legacy, None)
        db.update_session_tool_names("owned", None)
    else:
        scope = _scope(db)
        with pytest.raises(RuntimeStoreError):
            mutate_worker_execution(db, **scope, sequence=1, operation="session.tools", payload={"tool_names": _pin(["terminal"])})
        assert _raw(db) == (legacy, None)
        mutate_worker_execution(db, **scope, sequence=1, operation="session.tools", payload={"tool_names": None})
    agent = _receiver(db)
    _restore_pinned_tools(agent, db.get_session("owned"))
    assert agent.allowed_tools == ["read_file"]
    assert agent.valid_tool_names == {"read_file"}
