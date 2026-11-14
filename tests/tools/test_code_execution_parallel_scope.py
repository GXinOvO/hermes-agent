import contextvars
import json
import threading
from unittest.mock import patch
from typing import Any

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels


def test_parallel_workers_keep_profile_scope_and_callbacks_isolated(tmp_path, monkeypatch):
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.thread_context import _callback_api

    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    marker = contextvars.ContextVar("parallel-scope-marker")
    rendezvous = threading.Barrier(2, timeout=5)
    observed = []
    callback_api: Any = _callback_api()
    originals = [getter() for getter, _setter in callback_api]
    code = "from hermes_tools import parallel\nparallel([{'tool':'read_file','args':{'path':str(i)}} for i in range(2)])"

    def dispatch(name, args, task_id=None, **kwargs):
        before = marker.get()
        observed.append((str(get_hermes_home()), before, task_id,
                         tuple(getter() for getter, _setter in callback_api)))
        marker.set("worker-local-change")
        rendezvous.wait()
        return "{}"

    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict"}), \
                patch("model_tools.handle_function_call", side_effect=dispatch):
            for index in range(2):
                profile = tmp_path / f"profile-{index}"
                profile.mkdir()
                home_token = set_hermes_home_override(profile)
                session_token = set_current_session_key(f"scope-owner-{index}")
                states = {
                    'set_current_session_id': f'vault-owner-{index}',
                    '_set_tool_whitelist': (frozenset({'read_file'}), f'blocked-scope-{index}'),
                }
                values = tuple(states.get(setter.__name__, lambda i=i: i)
                               for i, (_getter, setter) in enumerate(callback_api))
                try:
                    marker.set(f"scope-{index}")
                    for (_getter, setter), value in zip(callback_api, values):
                        setter(value)
                    result = json.loads(_execute_code_handler(
                        {"code": code}, task_id=f"scope-task-{index}", enabled_tools=["read_file"],
                    ))
                    assert result["status"] == "success", result
                    assert marker.get() == f"scope-{index}"
                    expected = (str(profile), f"scope-{index}", f"scope-task-{index}", values)
                    assert observed[-2:] == [expected, expected]
                finally:
                    reset_current_session_key(session_token)
                    reset_hermes_home_override(home_token)
    finally:
        for (_getter, setter), original in zip(callback_api, originals):
            setter(original)
        shutdown_all_kernels()
