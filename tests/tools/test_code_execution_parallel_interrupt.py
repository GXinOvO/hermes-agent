import json
import threading
from unittest.mock import patch

from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels


def test_native_interrupt_stops_queued_batch_and_other_owner_stays_usable(monkeypatch):
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.interrupt import is_interrupted, set_interrupt

    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    entered = threading.Barrier(5, timeout=5)
    rescue = threading.Event()
    completed = threading.Event()
    retired = threading.Event()
    seen, outcomes, owner_tid = [], [], []
    guard = threading.Lock()

    def dispatch(name, args, task_id=None):
        if task_id == "other-task":
            return '{"content":"other-owner"}'
        with guard:
            seen.append(args["path"])
        entered.wait()
        while not is_interrupted() and not rescue.is_set():
            rescue.wait(0.01)
        with guard:
            outcomes.append(is_interrupted())
            if len(outcomes) == 4:
                retired.set()
        return "{}"

    code = (
        "from hermes_tools import parallel\n"
        "parallel([{'tool':'read_file','args':{'path':str(i)}} for i in range(8)])"
    )
    result = []

    def execute():
        token = set_current_session_key("interrupted-batch-owner")
        owner_tid.append(threading.get_ident())
        try:
            result.append(json.loads(_execute_code_handler(
                {"code": code}, task_id="interrupted-task", enabled_tools=["read_file"],
            )))
        finally:
            reset_current_session_key(token)
            completed.set()

    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "timeout": 20}), \
            patch("model_tools.handle_function_call", side_effect=dispatch):
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            entered.wait()
            set_interrupt(True, owner_tid[0])
            assert completed.wait(5), "native execute_code did not stop"
            assert retired.wait(5), "workers did not observe the original owner's interrupt"
            assert result[0]["status"] == "interrupted", result
            assert result[0]["tool_calls_made"] == 4
            assert outcomes == [True] * 4
            assert len(seen) == 4
            token = set_current_session_key("uninterrupted-owner")
            try:
                other = json.loads(_execute_code_handler(
                    {"code": "from hermes_tools import read_file\nprint(read_file('ok'))"},
                    task_id="other-task", enabled_tools=["read_file"],
                ))
            finally:
                reset_current_session_key(token)
            assert other["status"] == "success", other
            assert "other-owner" in other["output"]
        finally:
            rescue.set()
            thread.join(10)
            if owner_tid:
                set_interrupt(False, owner_tid[0])
            shutdown_all_kernels()
        assert not thread.is_alive()
