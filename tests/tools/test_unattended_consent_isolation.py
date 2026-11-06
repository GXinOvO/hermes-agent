"""Unattended consent isolation, exercised with native stores and inert commands."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import config

COMMAND = "git reset --hard HEAD"

# The prompt is the human input seam; detection, persistence, config and guards
# remain real. Each invocation imports the subject in a fresh process.
CHILD = """
import json, sys
from unittest.mock import patch
from tools import approval as a, approval_context as ctx
ctx.set_hermes_interactive_context(sys.argv[1] == 'human')
with patch.object(a, 'prompt_dangerous_approval', return_value='always'):
    result = a.check_all_command_guards(sys.argv[2], 'local')
print(json.dumps(result))
"""


@pytest.fixture
def consent_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config.atomic_config_replace(home / "config.yaml", {
        "model": {"default": "test-model"},
        "approvals": {"mode": "manual", "cron_mode": "deny",
                      "single_query_mode": "deny", "unattended_mode": "deny",
                      "unattended_inherit_approvals": False},
        "command_allowlist": [],
    })
    config._LOAD_CONFIG_CACHE.clear()
    yield home
    config._LOAD_CONFIG_CACHE.clear()


def child_decision(home, context):
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "TMPDIR")
           if key in os.environ}
    env.update(HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_INTERACTIVE="1" if context in {"human", "single_query"} else "",
               HERMES_CRON_SESSION="1" if context == "cron" else "",
               HERMES_SINGLE_QUERY_SESSION="1" if context == "single_query" else "",
               HERMES_SESSION_PLATFORM=context if context in {"webhook", "msgraph_webhook", "api_server"} else "")
    result = subprocess.run([sys.executable, "-c", CHILD, context, COMMAND],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_interactive_always_is_not_inherited_by_fresh_cron(consent_home):
    assert child_decision(consent_home, "human")["approved"] is True
    saved = config.read_user_config_raw()["command_allowlist"]
    assert saved, "native human Always must persist the detected pattern key"
    result = child_decision(consent_home, "cron")
    assert result["approved"] is False, result
    assert "cron" in result["message"]
    assert config.read_user_config_raw()["command_allowlist"] == saved


@pytest.mark.parametrize("context", ["single_query", "webhook", "msgraph_webhook", "api_server"])
def test_fresh_unattended_surfaces_do_not_inherit_always(consent_home, context):
    assert child_decision(consent_home, "human")["approved"] is True
    assert child_decision(consent_home, context)["approved"] is False


def configure(home, **settings):
    data = config.read_user_config_raw()
    data["approvals"].update(settings)
    config.atomic_config_replace(home / "config.yaml", data)
    config._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def native_state(consent_home, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import approval as a, approval_context as ctx
    token = set_hermes_home_override(consent_home)
    session = ctx.set_current_session_key("isolation-session")
    interactive = ctx.set_hermes_interactive_context(True)
    route = set_session_vars(platform="", cron_session="")
    for name in ("HERMES_EXEC_ASK", "HERMES_SINGLE_QUERY_SESSION", "HERMES_GATEWAY_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(a, "_YOLO_MODE_FROZEN", False)
    a.clear_session("isolation-session")
    a.load_permanent_allowlist()
    yield a, ctx
    a.clear_session("isolation-session")
    clear_session_vars(route)
    ctx.reset_hermes_interactive_context(interactive)
    ctx.reset_current_session_key(session)
    reset_hermes_home_override(token)


def human_answer(a, monkeypatch, choice):
    def answer(*args, **kwargs):
        return choice
    monkeypatch.setattr(a, "prompt_dangerous_approval", answer)


def enter_headless(ctx, monkeypatch, context):
    # native_state resets the original public bindings after this test.
    from gateway.session_context import set_session_vars
    ctx.set_hermes_interactive_context(False)
    set_session_vars(platform="webhook" if context == "webhook" else "",
                     cron_session="1" if context == "cron" else "")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1" if context == "single_query" else "")


@pytest.mark.parametrize("entry", ["text", "glob", "canonical", "legacy"])
@pytest.mark.parametrize("context", ["cron", "single_query", "webhook"])
def test_command_entries_are_ineligible_without_erasing_raw_store(consent_home, native_state, monkeypatch, entry, context):
    a, ctx = native_state
    from tools.approval_detection import _approval_key_aliases
    key = a.detect_dangerous_command(COMMAND)[1]
    legacy = next(iter(_approval_key_aliases(key) - {key}))
    value = {"text": COMMAND, "glob": "git reset --hard *", "canonical": key, "legacy": legacy}[entry]
    a.approve_permanent(value)
    a.save_permanent_allowlist({value})
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    before = config.read_user_config_raw()["command_allowlist"]
    enter_headless(ctx, monkeypatch, context)
    assert not a.check_all_command_guards(COMMAND, "local")["approved"]
    assert not a.check_dangerous_command(COMMAND, "local")["approved"]
    assert value in a._permanent_set()
    assert config.read_user_config_raw()["command_allowlist"] == before
    assert a.check_all_command_guards("git status --short", "local")["approved"]


@pytest.mark.parametrize("choice", ["once", "session", "always"])
@pytest.mark.parametrize("gate", ["shell", "action", "file"])
def test_interactive_consent_still_works(consent_home, native_state, monkeypatch, choice, gate):
    a, _ = native_state
    human_answer(a, monkeypatch, choice)
    from tools.file_tools_write_guards import _check_approval_required_write
    target = str(consent_home.parent / ".ssh" / "config")
    calls = {"shell": lambda: a.check_all_command_guards(COMMAND, "local")["approved"],
             "action": lambda: a.request_tool_approval("write_file", "test approval", rule_key="isolation")["approved"],
             "file": lambda: _check_approval_required_write([target]) is None}
    assert calls[gate]()
    human_answer(a, monkeypatch, "deny")
    assert calls[gate]() is (choice != "once")
    saved = config.read_user_config_raw()["command_allowlist"]
    assert bool(saved) is (choice == "always")


@pytest.mark.parametrize("choice", ["session", "always"])
@pytest.mark.parametrize("context", ["cron", "single_query", "webhook"])
@pytest.mark.parametrize("gate", ["action", "file"])
def test_action_cache_does_not_cross_into_headless(consent_home, native_state, monkeypatch, choice, context, gate):
    a, ctx = native_state
    human_answer(a, monkeypatch, choice)
    from tools.file_tools_write_guards import _check_approval_required_write
    target = str(consent_home.parent / ".ssh" / "config")
    call = (lambda: a.request_tool_approval("write_file", "test approval", rule_key="isolation")["approved"]) if gate == "action" else (lambda: _check_approval_required_write([target]) is None)
    assert call()
    enter_headless(ctx, monkeypatch, context)
    assert not call()


@pytest.mark.parametrize("context,mode", [("cron", "cron_mode"), ("single_query", "single_query_mode"), ("webhook", "unattended_mode")])
def test_explicit_approve_and_yolo_keep_native_meaning(consent_home, native_state, monkeypatch, context, mode):
    a, ctx = native_state
    enter_headless(ctx, monkeypatch, context)
    configure(consent_home, **{mode: "approve"})
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    assert a.check_dangerous_command(COMMAND, "local")["approved"]
    assert a.request_tool_approval("write_file", "test approval", rule_key="explicit")["approved"]
    assert a.check_execute_code_guard("print('inert')", "local")["approved"]
    configure(consent_home, **{mode: "deny"})
    a.enable_session_yolo("isolation-session")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    assert a.request_tool_approval("write_file", "test approval", rule_key="explicit")["approved"]
    assert a.check_execute_code_guard("print('inert')", "local")["approved"]


@pytest.mark.parametrize("inherit", [False, True])
def test_execute_code_headless_deny_stays_before_saved_grants(consent_home, native_state, monkeypatch, inherit):
    a, ctx = native_state
    configure(consent_home, unattended_inherit_approvals=inherit)
    a._persist_choice("isolation-session", "always", ["execute_code"])
    enter_headless(ctx, monkeypatch, "cron")
    assert not a.check_execute_code_guard("print('inert')", "local")["approved"]


@pytest.mark.parametrize("inherit", [None, True])
def test_compatibility_default_and_explicit_true(consent_home, inherit):
    assert child_decision(consent_home, "human")["approved"]
    data = config.read_user_config_raw()
    if inherit is None:
        data["approvals"].pop("unattended_inherit_approvals")
    else:
        data["approvals"]["unattended_inherit_approvals"] = inherit
    config.atomic_config_replace(consent_home / "config.yaml", data)
    assert child_decision(consent_home, "cron")["approved"]


def test_revoke_then_save_does_not_resurrect_old_grants(consent_home, native_state, monkeypatch):
    a, _ = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    old = set(config.read_user_config_raw()["command_allowlist"])
    data = config.read_user_config_raw()
    data["command_allowlist"] = []
    config.atomic_config_replace(consent_home / "config.yaml", data)
    config._LOAD_CONFIG_CACHE.clear()
    a.clear_session("isolation-session")
    a.save_permanent_allowlist({"another grant"})
    assert not (set(config.read_user_config_raw()["command_allowlist"]) & old)
    assert not (a._permanent_set() & old)


@pytest.mark.parametrize("context", ["cron", "single_query", "webhook"])
def test_prepared_human_guard_is_consumed_but_regated_headlessly(consent_home, native_state, monkeypatch, context):
    from types import SimpleNamespace
    from agent import terminal_approval_batch as batch
    a, ctx = native_state
    human_answer(a, monkeypatch, "once")
    decision = a.check_all_command_guards(COMMAND, "local")
    assert decision["approved"] and decision["user_approved"]
    slot = SimpleNamespace(preparing=False, decision=decision, guard_key=(COMMAND, "local", False),
                           batch=SimpleNamespace(failure_seen=False, task_id="isolation"),
                           parsed=SimpleNamespace(ref=lambda task_id: SimpleNamespace(call_id="prepared")),
                           check_cancelled=lambda: None)
    token = batch._slot.set(slot)
    ids = ctx.set_current_observability_context(tool_call_id="prepared")
    try:
        enter_headless(ctx, monkeypatch, context)
        assert not a.check_all_command_guards(COMMAND, "local")["approved"]
        assert slot.decision is None
    finally:
        ctx.reset_current_observability_context(ids)
        batch._slot.reset(token)


def test_served_profiles_resolve_own_config_and_permanent_grants(consent_home, native_state, monkeypatch, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    a, ctx = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    saved = config.read_user_config_raw()["command_allowlist"]
    other = tmp_path / "secondary"
    other.mkdir()
    data = config.read_user_config_raw()
    data["command_allowlist"] = []
    data["approvals"]["unattended_inherit_approvals"] = True
    config.atomic_config_replace(other / "config.yaml", data)
    enter_headless(ctx, monkeypatch, "cron")
    token = set_hermes_home_override(other)
    try:
        assert not a.check_all_command_guards(COMMAND, "local")["approved"]
        a.approve_permanent("secondary grant")
        a.save_permanent_allowlist({"secondary grant"})
        assert config.read_user_config_raw()["command_allowlist"] == ["secondary grant"]
    finally:
        reset_hermes_home_override(token)
    assert not a.check_all_command_guards(COMMAND, "local")["approved"]
    assert config.read_user_config_raw()["command_allowlist"] == saved


def test_api_client_with_answerable_card_keeps_interactive_consent(consent_home, native_state, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    a, ctx = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    route = set_session_vars(platform="api_server", cron_session="")
    interactive = ctx.set_hermes_interactive_context(False)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    a.register_gateway_notify("isolation-session", lambda data: None)
    try:
        assert a.check_all_command_guards(COMMAND, "local")["approved"]
    finally:
        a.unregister_gateway_notify("isolation-session")
        ctx.reset_hermes_interactive_context(interactive)
        clear_session_vars(route)


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook", "api_server"])
def test_inherited_presence_flags_do_not_make_automation_interactive(consent_home, native_state, monkeypatch, platform):
    from gateway.session_context import set_session_vars, clear_session_vars
    a, _ = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    route = set_session_vars(platform=platform, cron_session="")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    # A webhook's generic TurnRunner notifier cannot receive a human answer;
    # an API request with no client notifier is likewise unattended.
    if platform != "api_server":
        a.register_gateway_notify("isolation-session", lambda data: None)
    try:
        assert not a.is_approved("isolation-session", a.detect_dangerous_command(COMMAND)[1])
    finally:
        a.unregister_gateway_notify("isolation-session")
        clear_session_vars(route)


@pytest.mark.parametrize("context", ["cron", "single_query", "webhook"])
@pytest.mark.parametrize("gate", ["text", "glob", "canonical", "legacy", "session", "action", "file", "prepared"])
def test_config_version_failure_does_not_restore_inheritance(consent_home, native_state, monkeypatch, context, gate):
    from types import SimpleNamespace
    from agent import terminal_approval_batch as batch
    from hermes_cli.config_backend import FileBackend
    from tools.approval_detection import _approval_key_aliases
    from tools.file_tools_write_guards import _check_approval_required_write
    a, ctx = native_state
    human_answer(a, monkeypatch, {"session": "session", "prepared": "once"}.get(gate, "always"))
    key = a.detect_dangerous_command(COMMAND)[1]
    legacy = next(iter(_approval_key_aliases(key) - {key}))
    if gate in {"text", "glob", "canonical", "legacy"}:
        value = {"text": COMMAND, "glob": "git reset --hard *", "canonical": key, "legacy": legacy}[gate]
        a.approve_permanent(value)
        a.save_permanent_allowlist({value})
    target = str(consent_home.parent / ".ssh" / "config")
    call = {"action": lambda: a.request_tool_approval("write_file", "config failure", rule_key="config-failure")["approved"],
            "file": lambda: _check_approval_required_write([target]) is None}.get(gate, lambda: a.check_all_command_guards(COMMAND, "local")["approved"])
    assert call()
    slot_token = ids = None
    if gate == "prepared":
        decision = a.check_all_command_guards(COMMAND, "local")
        assert decision["approved"] and decision["user_approved"]
        slot = SimpleNamespace(preparing=False, decision=decision, guard_key=(COMMAND, "local", False),
                               batch=SimpleNamespace(failure_seen=False, task_id="isolation"),
                               parsed=SimpleNamespace(ref=lambda task_id: SimpleNamespace(call_id="config-failure")),
                               check_cancelled=lambda: None)
        slot_token = batch._slot.set(slot)
        ids = ctx.set_current_observability_context(tool_call_id="config-failure")
    enter_headless(ctx, monkeypatch, context)
    version = FileBackend.version_path
    def unavailable(path):
        if Path(path) == consent_home / "config.yaml":
            raise PermissionError("synthetic config version unavailable")
        return version(path)
    try:
        if gate != "prepared":
            assert not call()
        with monkeypatch.context() as failed:
            failed.setattr(FileBackend, "version_path", staticmethod(unavailable))
            assert not call()
            assert not ctx._can_inherit_approvals()
        assert not call()
        if gate not in {"session", "prepared"}:
            configure(consent_home, unattended_inherit_approvals=True)
            assert call()
    finally:
        if ids is not None:
            ctx.reset_current_observability_context(ids)
        if slot_token is not None:
            batch._slot.reset(slot_token)


def test_config_load_failure_closes_only_automatic_inheritance(consent_home, native_state, monkeypatch):
    a, ctx = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    enter_headless(ctx, monkeypatch, "cron")
    with monkeypatch.context() as failed:
        failed.setattr(config, "load_config_readonly", lambda: config.FailedConfigRead({"approvals": {"unattended_inherit_approvals": True}}, error=OSError("synthetic read failure")))
        assert not ctx._can_inherit_approvals()
    configure(consent_home, unattended_inherit_approvals=True)
    assert a.check_all_command_guards(COMMAND, "local")["approved"]


def test_profile_config_failure_does_not_change_other_profile(consent_home, native_state, monkeypatch, tmp_path):
    from hermes_cli.config_backend import FileBackend
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    a, ctx = native_state
    human_answer(a, monkeypatch, "always")
    assert a.check_all_command_guards(COMMAND, "local")["approved"]
    other = tmp_path / "readable-profile"
    other.mkdir()
    data = config.read_user_config_raw()
    data["approvals"].pop("unattended_inherit_approvals")
    config.atomic_config_replace(other / "config.yaml", data)
    enter_headless(ctx, monkeypatch, "cron")
    version = FileBackend.version_path
    def unavailable(path):
        if Path(path) == consent_home / "config.yaml":
            raise OSError("synthetic config version I/O error")
        return version(path)
    with monkeypatch.context() as failed:
        failed.setattr(FileBackend, "version_path", staticmethod(unavailable))
        assert not a.check_all_command_guards(COMMAND, "local")["approved"]
        token = set_hermes_home_override(other)
        try:
            assert ctx._can_inherit_approvals()
            assert a.check_all_command_guards(COMMAND, "local")["approved"]
        finally:
            reset_hermes_home_override(token)
        assert not a.check_all_command_guards(COMMAND, "local")["approved"]
    assert not ctx._can_inherit_approvals()
