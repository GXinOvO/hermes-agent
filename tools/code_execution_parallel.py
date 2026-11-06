"""Bounded batches of independent read-only calls over the existing tool RPC."""

import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.registry import tool_error
from tools.thread_context import propagate_context_to_thread

PARALLEL_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})
MAX_PARALLEL_CALLS = 16
MAX_PARALLEL_WORKERS = 4
logger = logging.getLogger(__name__)


def execute_parallel(args, *, allowed_tools, tool_call_counter, max_tool_calls,
                     dispatch, tool_call_log, stop_event):
    from tools.code_execution_rpc import _handle_rpc_request

    calls = args.get("calls") if isinstance(args, dict) else None
    if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_PARALLEL_CALLS:
        return tool_error(f"parallel requires 1-{MAX_PARALLEL_CALLS} read-only calls.")
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"tool", "args"}
                or not isinstance(call["tool"], str)
                or call["tool"] not in PARALLEL_TOOLS & allowed_tools
                or not isinstance(call["args"], dict)):
            return tool_error("parallel accepts only enabled read_file, search_files, "
                              "web_search and web_extract calls with tool/args fields.")
    if len(calls) > max_tool_calls - tool_call_counter[0]:
        return tool_error(f"Tool call limit reached ({max_tool_calls}): "
                          "parallel batch does not fit the remaining budget; no calls ran.")

    authority = getattr(dispatch, "__self__", None)
    cell_stop = getattr(authority, "stop_event", None)
    budget_lock = threading.Lock()

    def stopped():
        return ((stop_event is not None and stop_event.is_set())
                or (cell_stop is not None and cell_stop.is_set()))

    def invoke(call):
        return json.loads(_handle_rpc_request(
            call, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
            max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
            call_start=time.monotonic(), where="parallel sandbox", stop_event=stop_event,
            budget_lock=budget_lock,
        ))

    pool = None
    futures = []
    try:
        pool = DaemonThreadPoolExecutor(max_workers=min(MAX_PARALLEL_WORKERS, len(calls)))
        for call in calls:
            futures.append(pool.submit(propagate_context_to_thread(invoke), call))
        pending = set(futures)
        while True:
            if stopped():
                return tool_error("execute_code stopped; unfinished reads may still be running.")
            if not pending:
                break
            _, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        return json.dumps([future.result() for future in futures], ensure_ascii=True)
    except Exception as exc:
        logger.exception("parallel batch failed")
        return tool_error(f"parallel batch failed: {type(exc).__name__}: {exc}")
    finally:
        for future in futures:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
