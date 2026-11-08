"""Host-side RPC servers for execute_code sandboxes.

Two transports share one request pipeline (token check → allow-list → call
budget → dispatch under output silence → log): ``_rpc_server_loop`` serves the
local UDS/TCP socket, ``_rpc_poll_loop`` polls a remote filesystem for request
files via ``env.execute()``.
"""

import base64
from contextlib import nullcontext
from contextvars import ContextVar
import json
import logging
import secrets
import shlex
import socket
import threading
import time

from agent.thread_scoped_output import thread_scoped_silence
from tools.registry import tool_error

# Logger name kept as the origin module's so existing log expectations hold.
logger = logging.getLogger("tools.code_execution_tool")

# Terminal parameters that must not be used from ephemeral sandbox scripts.
_TERMINAL_BLOCKED_PARAMS = {"background", "pty", "notify", "notify_on_complete", "watch_patterns", "heartbeat", "persist_on_release"}
_parallel_call_id: ContextVar[str] = ContextVar("ptc_parallel_call_id", default="")


def _default_dispatch(task_id):
    from model_tools import handle_function_call
    def dispatch(tool_name, tool_args):
        if call_id := _parallel_call_id.get():
            from tools.approval_context import (get_current_observability_context,
                                                reset_current_observability_context,
                                                set_current_observability_context)
            scope = get_current_observability_context()
            scope["tool_call_id"] = call_id
            tokens = set_current_observability_context(**scope)
            try:
                return handle_function_call(tool_name, tool_args, task_id=task_id,
                                            tool_call_id=call_id, turn_id=scope["turn_id"],
                                            session_id=scope["session_id"])
            finally:
                reset_current_observability_context(tokens)
        return handle_function_call(tool_name, tool_args, task_id=task_id)
    return dispatch


def _private_dirs_cmd(root: str, *subdirs: str) -> str:
    """Shell command creating *root* (and optional *subdirs* under it) owner-only
    on a shared host. ``umask 077`` makes intermediates and leaves private at
    creation (no mkdir-then-chmod window where a co-tenant could open a dir fd);
    ``chmod`` then repairs any named dir that already existed with permissive
    modes."""
    mkdir = " ".join(shlex.quote(d) for d in (subdirs or (root,)))
    chmod = " ".join(shlex.quote(d) for d in (root, *subdirs))
    return f"umask 077 && mkdir -p {mkdir} && chmod 700 {chmod}"


def _execute_checked(env, cmd: str, what: str, *, timeout: int, **kwargs) -> dict:
    """Run *cmd* from ``/`` and raise ``RuntimeError`` on a non-zero exit.

    Used where a silent failure would ship secrets or code into a missing,
    half-written, or still-permissive remote path. The error carries the
    command output only, never the payload."""
    result = env.execute(cmd, cwd="/", timeout=timeout, **kwargs)
    if result.get("returncode", 1) != 0:
        raise RuntimeError(f"{what} failed: {result.get('output')!r}")
    return result


def _remote_write(env, remote_path: str, content: str, *, atomic: bool = False,
                  timeout: int = 30, check: bool = False):
    """Write *content* owner-only to *remote_path*; returns the execute() result
    (``check=True`` raises on failure via _execute_checked).

    The base64 payload always travels as ``stdin_data``: pipe-mode backends
    (ssh, docker, local: the real shared-host ones) deliver it on
    real stdin, so it never enters argv where a co-tenant can read it via
    ``/proc/*/cmdline``; ``BaseEnvironment.execute`` embeds it as a heredoc
    for heredoc-mode backends (modal, daytona, vercel), and managed_modal
    forwards it as ``stdinData``, the same contract ``_write_to_sandbox``
    relies on."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    target = shlex.quote(remote_path)
    write = (f"base64 -d > {target}.tmp && mv -f {target}.tmp {target}"
             if atomic else f"base64 -d > {target}")
    cmd = f"umask 077 && {write}"
    if check:
        return _execute_checked(env, cmd, f"remote file ship for {remote_path!r}",
                                timeout=timeout, stdin_data=encoded)
    return env.execute(cmd, cwd="/", timeout=timeout, stdin_data=encoded)


def _rpc_token_ok(request: dict, rpc_token: str) -> bool:
    """Constant-time token check; an empty server token fails closed. Compared as bytes:
    compare_digest raises TypeError on a non-ASCII str, and the token is script-supplied JSON."""
    if not isinstance(request, dict) or not rpc_token:
        return False
    supplied = request.get("token")
    if not isinstance(supplied, str):
        return False
    try:
        return secrets.compare_digest(supplied.encode(), rpc_token.encode())
    except UnicodeEncodeError:
        return False


def _handle_rpc_request(request: dict, *, allowed_tools: frozenset, tool_call_counter: list,
                        max_tool_calls: int, dispatch, tool_call_log: list, call_start: float,
                        where: str, stop_event=None, budget_lock=None,
                        allow_parallel=False) -> str:
    """Enforce allow-list + budget, then dispatch one authenticated request. Only a dispatched
    call consumes budget and is logged; refusals are free."""
    if (not isinstance(request, dict) or not isinstance(request.get("tool"), str)
            or not isinstance(request.get("args", {}), dict)):
        return tool_error("Invalid RPC request: expected a tool name and an args object.")
    tool_name = request.get("tool", "")
    tool_args = request.get("args", {})
    authority = getattr(dispatch, "__self__", None)
    authority = authority if hasattr(authority, "_dispatch_started") else None
    if authority is not None:
        with authority.admission_lock:
            if not authority.active:
                return tool_error("No active execute_code cell: its tool authority is retired.")
    if tool_name == "__parallel__":
        if not allow_parallel:
            return tool_error("parallel is only available over the local execute_code transport.")
        from tools.code_execution_parallel import execute_parallel
        return execute_parallel(
            tool_args, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
            max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
            stop_event=stop_event,
        )
    if tool_name not in allowed_tools:
        return tool_error(f"Tool '{tool_name}' is not available in execute_code. "
                          f"Available: {', '.join(sorted(allowed_tools))}")
    lock = authority.admission_lock if authority is not None else budget_lock
    with lock if lock is not None else nullcontext():
        if authority is not None and not authority.active:
            return tool_error("No active execute_code cell: its tool authority is retired.")
        if authority is not None:
            from tools.interrupt import is_thread_interrupted
            if is_thread_interrupted(authority.origin_tid):
                return tool_error("execute_code interrupted before this tool call was dispatched.")
        if stop_event is not None and stop_event.is_set():
            return tool_error("execute_code stopped before this tool call was dispatched.")
        if tool_call_counter[0] >= max_tool_calls:
            return tool_error(f"Tool call limit reached ({max_tool_calls}). "
                              "No more tool calls allowed in this execution.")
        tool_call_counter[0] += 1
    if tool_name == "terminal" and isinstance(tool_args, dict):
        for param in _TERMINAL_BLOCKED_PARAMS:
            tool_args.pop(param, None)
    # Silence handler status prints so they don't leak into the CLI spinner.
    try:
        with thread_scoped_silence():
            runner = authority._dispatch_started if authority is not None else dispatch
            result = runner(tool_name, tool_args)
    except Exception as exc:
        logger.error("Tool call failed in %s: %s", where, exc, exc_info=True)
        result = tool_error(str(exc))
    entry = {"tool": tool_name, "args_preview": str(tool_args)[:80],
             "duration": round(time.monotonic() - call_start, 2)}
    error = _result_error(result)
    if error:
        entry["error"] = error
    tool_call_log.append(entry)
    return _serialize_rpc_result(result)


def _serialize_rpc_result(result) -> str:
    """Keep one JSON wire value even when a result hook returns multiline text."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            pass  # A transform hook may return text rather than a JSON document.
    # JSON escapes preserve surrogate-containing paths/text through UTF-8 transports.
    return json.dumps(result, ensure_ascii=True)


def _result_error(result) -> str:
    """The ``error`` text of a JSON-object tool result, else ``""``."""
    if not isinstance(result, str) or not result.startswith("{") or '"error"' not in result:
        return ""
    try:
        body = json.loads(result)
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error)[:300] if error else ""


def tool_errors_since(tool_call_log: list, start: int = 0) -> list:
    """Failed in-script tool calls since *start*, for the execute_code result: a script that
    ignores a helper's ``{"error": ...}`` return would otherwise report plain success while
    the write/patch it relied on never happened."""
    return [{"tool": e["tool"], "error": e["error"]} for e in tool_call_log[start:] if e.get("error")][:5]


def _rpc_server_loop(server_sock: socket.socket, task_id: str, tool_call_log: list,
                     tool_call_counter: list, max_tool_calls: int, allowed_tools: frozenset,
                     stop_event: threading.Event, rpc_token: str, dispatch=None,
                     request_dispatch=None):
    """Accept one client and serve newline-delimited JSON requests until it disconnects, idles
    300s, or the call limit is reached. ``tool_call_counter`` is a mutable ``[int]``. ``dispatch``
    overrides how an allowed, budgeted call runs: per-call sandboxes use the default (the thread
    carries the cell's context); session kernels rebind each call to the CURRENT cell's authority.
    """
    if dispatch is None:
        dispatch = _default_dispatch(task_id)
    conn = None
    try:
        server_sock.settimeout(0.05)
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break
            except TimeoutError:
                continue
        if conn is None:
            return
        conn.settimeout(300)
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                call_start = time.monotonic()
                try:
                    request = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    resp = tool_error(f"Invalid RPC request: {exc}")
                else:
                    if not _rpc_token_ok(request, rpc_token):
                        resp = tool_error("Unauthorized RPC request")
                    elif request_dispatch is not None:
                        resp = request_dispatch(request)
                    else:
                        resp = _handle_rpc_request(
                            request, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
                            max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
                            call_start=call_start, where="sandbox", stop_event=stop_event,
                            allow_parallel=True,
                        )
                conn.sendall((resp + "\n").encode())
    except TimeoutError:
        logger.debug("RPC listener socket timeout")
    except OSError as e:
        logger.debug("RPC listener socket error: %s", e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except OSError as e:
                logger.debug("RPC conn close error: %s", e)


def _rpc_poll_loop(env, rpc_dir: str, task_id: str, tool_call_log: list, tool_call_counter: list,
                   max_tool_calls: int, allowed_tools: frozenset, stop_event: threading.Event,
                   rpc_token: str):
    """Poll the remote filesystem for request files and answer them. Background thread; each
    ``env.execute()`` is an independent process, so this is safe alongside the script-execution
    thread. Malformed or unauthorized requests are removed without a response."""
    dispatch = _default_dispatch(task_id)
    poll_interval = 0.1
    quoted_rpc_dir = shlex.quote(rpc_dir)
    while not stop_event.is_set():
        try:
            ls_result = env.execute(f"ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true", cwd="/", timeout=10)
            output = ls_result.get("output", "").strip()
            if not output:
                stop_event.wait(poll_interval)
                continue
            req_files = sorted(f for f in (line.strip() for line in output.split("\n"))
                               if f and not f.endswith(".tmp") and "/req_" in f)
            for req_file in req_files:
                if stop_event.is_set():
                    break
                call_start = time.monotonic()
                quoted_req_file = shlex.quote(req_file)
                read_result = env.execute(f"cat {quoted_req_file}", cwd="/", timeout=10)
                try:
                    request = json.loads(read_result.get("output", ""))
                except (json.JSONDecodeError, ValueError):
                    logger.debug("Malformed RPC request in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                if not _rpc_token_ok(request, rpc_token):
                    logger.debug("Unauthorized RPC request in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                seq = request.get("seq", 0)
                if not isinstance(seq, int):
                    # A non-int seq cannot form the res_NNNNNN name the caller
                    # polls; formatting it after dispatch would raise, leave the
                    # request in place, and replay the tool call every cycle.
                    logger.debug("RPC request with malformed seq in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                tool_result = _handle_rpc_request(
                    request, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
                    max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
                    call_start=call_start, where="remote sandbox",
                    stop_event=stop_event,
                )
                # Atomic (tmp + rename) and owner-only; results carry tool output
                # on a shared-host backend.
                _remote_write(env, f"{rpc_dir}/res_{seq:06d}", tool_result,
                              atomic=True, timeout=60)
                env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
        except Exception as e:
            if not stop_event.is_set():
                logger.debug("RPC poll error: %s", e, exc_info=True)
        if not stop_event.is_set():
            stop_event.wait(poll_interval)
