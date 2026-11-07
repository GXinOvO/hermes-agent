"""Persistent side-result boundary for the standalone TUI/desktop sidecar."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _stored_id(session):
    # Same durable target as off-turn submit persistence, including a rotated agent tip.
    return str(getattr(session.get("agent"), "session_id", None) or session.get("session_key") or "")


def _store_path(session):
    from hermes_state import SessionDB
    from tui_gateway import server
    agent_db = getattr(session.get("agent"), "_session_db", None)
    home = session.get("profile_home")
    if isinstance(agent_db, SessionDB):
        path = Path(agent_db.db_path).resolve()
        if home and path.parent != Path(home).resolve():
            raise ValueError("background profile identity conflict")
        return path
    if home:
        return Path(home).resolve() / "state.db"
    db = server._get_db()
    if not isinstance(db, SessionDB):
        raise RuntimeError("background result storage unavailable")
    return Path(db.db_path).resolve()


class SideResult:
    """Hold an independent registry reference until sealing and publication finish."""

    def __init__(self, db, task_id, binding):
        self.db, self.task_id, self.binding = db, task_id, binding

    def close(self):
        from hermes_state_registry import release_or_close
        release_or_close(self.db)

    def produce(self, body, extra):
        from hermes_state_background import complete
        try:
            text = body()
        except Exception as exc:
            logger.debug("standalone side-result producer failed", exc_info=True)
            text = f"error: {exc}"
        return complete(self.db, self.task_id, self.binding,
                        {"task_id": self.task_id, **extra, "text": text})["payload"]

    def publish(self, emit, parent, payload):
        from hermes_state_background import claim_delivery, finish_delivery
        if not claim_delivery(self.db, self.task_id):
            return
        try:
            emit(self.binding["event_type"], parent, payload)
        except Exception:
            finish_delivery(self.db, self.task_id, "unknown")
            raise
        finish_delivery(self.db, self.task_id, "delivered")


def admit(session, task_id, event_type, *, inputs, cwd):
    from hermes_state_background import begin
    from hermes_state_registry import acquire, release_or_close
    parent = _stored_id(session)
    if not parent:
        raise ValueError("background stored parent identity missing")
    db = acquire(_store_path(session))
    try:
        agent = session.get("agent")
        binding = {"parent_session_id": parent, "profile_id": str(Path(db.db_path).parent.resolve()),
                   "event_type": event_type, "input_sha256": _digest(inputs),
                   "config_sha256": _digest({"cwd": cwd,
                       "runtime": {name: getattr(agent, name, None) for name in (
                           "model", "provider", "base_url", "api_key", "api_mode", "acp_command", "acp_args",
                           "reasoning_config", "service_tier", "enabled_toolsets", "disabled_toolsets",
                           "request_overrides", "_fallback_chain")}})}
        if not db.get_session(parent):
            db.create_session(parent, source=session.get("source") or "tui", cwd=cwd)
        record = begin(db, task_id, binding)
        if not record.get("claimed"):
            release_or_close(db)
            return None
        return SideResult(db, task_id, binding)
    except BaseException:
        release_or_close(db)
        raise


def attach_results(payload, session, *, db=None, parent_session_id=None):
    """Read the sealed list into a snapshot; never insert messages or start work."""
    from hermes_state import SessionDB
    from hermes_state_background import results
    from hermes_state_registry import acquire, release_or_close
    parent = parent_session_id or _stored_id(session)
    if not parent:
        return payload
    owned = None
    try:
        path = Path(db.db_path) if isinstance(db, SessionDB) else _store_path(session) if db is None else None
        if path is None or not path.is_file():
            return payload
        owned = acquire(path)
        payload["background_results"] = results(owned, parent)
    except Exception:
        logger.debug("background result read unavailable; no task restarted", exc_info=True)
    finally:
        if owned is not None:
            release_or_close(owned)
    return payload


def resume_response(ctx, sid, record, *, info, display=(), count_source=None, messages=None,
                    message_count=None, running=False, status="idle", hydrating=None,
                    started_at=None, auto_continue=None):
    from tui_gateway import server
    if messages is None:
        messages = ctx.messages(display)
    if message_count is None:
        message_count = len(count_source) if ctx.omit_messages else len(messages)
    payload = {"session_id": sid, "resumed": ctx.target, "message_count": message_count, "messages": messages,
               **({"messages_omitted": ctx.omit_messages} if hydrating is None else {"hydrating": hydrating}),
               "info": info, "inflight": None, "running": running, "session_key": ctx.target,
               "started_at": record["created_at"] if started_at is None else started_at, "status": status}
    if auto_continue is not None:
        payload["auto_continue"] = auto_continue
    attach_results(payload, record, db=ctx.db, parent_session_id=ctx.target)
    return server._ok(ctx.rid, server._attach_todo_state(payload, record))


def spawn(rid, session, task_id, parent, event, body, *, cwd="", extra=None, cleanup=None, inputs=None):
    from tui_gateway import server
    extra = extra or {}
    cwd = cwd or server._session_cwd(session)
    try:
        with server._session_profile_runtime_scope(session):
            side = admit(session, task_id, event, inputs=inputs or {}, cwd=cwd)
    except Exception as exc:
        logger.debug("standalone side-result admission failed", exc_info=True)
        return server._err(rid, 5035, str(exc))
    if side is None:
        return server._ok(rid, {"task_id": task_id})

    def run():
        tokens = server._set_session_context(task_id, cwd=cwd, ui_session_id=parent)
        try:
            with server._session_profile_runtime_scope(session):
                try:
                    payload = side.produce(body, extra)
                except Exception:
                    logger.debug("standalone side-result sealing failed", exc_info=True)
                    payload = {"task_id": task_id, **extra,
                               "text": "error: unknown_execution; result was not sealed; do not restart the task"}
                    server._emit(event, parent, payload)
                else:
                    side.publish(server._emit, parent, payload)
        finally:
            try:
                if cleanup is not None:
                    cleanup()
            finally:
                side.close()
                server._clear_session_context(tokens)

    try:
        if server._start_session_work(run, name=f"side-agent-{task_id}") is None:
            side.close()
            return server._err(rid, 5035, "backend is retiring; reconnect to continue")
    except BaseException:
        side.close()
        raise
    return server._ok(rid, {"task_id": task_id})
