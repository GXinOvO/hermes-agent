"""Durable side-result boundary for the classic CLI's in-process fallback."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _store(cli, *, create=False):
    from hermes_state import SessionDB
    db = getattr(cli, "_session_db", None)
    if db is None and create:
        from hermes_state_registry import acquire
        db = cli._session_db = acquire()
    if isinstance(db, SessionDB):
        return db
    if create:
        raise RuntimeError("background result storage unavailable; no task was started")
    return None


def _display_state(cli):
    lock = cli.__dict__.setdefault("_background_result_display_lock", threading.RLock())
    seen = cli.__dict__.setdefault("_background_results_shown", set())
    return lock, seen


def _identity(db, parent_session_id, task_id):
    return str(Path(db.db_path).resolve()), parent_session_id, task_id


class LocalSideResult:
    """Pin the store and parent before the worker can outlive a session switch."""

    def __init__(self, cli, db, task_id, binding):
        self.cli, self.db, self.task_id, self.binding = cli, db, task_id, binding
        self.sealed = False

    def produce(self, producer):
        from hermes_state_background import complete
        try:
            body = producer()
        except Exception as exc:
            saved = complete(self.db, self.task_id, self.binding,
                             {"task_id": self.task_id, "text": f"error: {exc}"})
            self.sealed = True
            raise RuntimeError(saved["payload"]["text"]) from exc
        saved = complete(self.db, self.task_id, self.binding,
                         {"task_id": self.task_id, "text": body or ""})
        self.sealed = True
        return saved["payload"]["text"]

    def display(self, printer):
        from hermes_state_background import claim_delivery, finish_delivery
        parent = self.binding["parent_session_id"]
        lock, seen = _display_state(self.cli)
        identity = _identity(self.db, parent, self.task_id)
        with lock:
            if getattr(self.cli, "session_id", parent) != parent or identity in seen:
                return False
            claimed = claim_delivery(self.db, self.task_id)
            if not claimed:
                return False
            try:
                printer()
            except Exception:
                finish_delivery(self.db, self.task_id, "unknown")
                raise
            seen.add(identity)
            finish_delivery(self.db, self.task_id, "delivered")
            return True


def admit(cli, task_id, event_type, *, inputs, route):
    """Only a fresh persistent identity may start a producer; a query never retries it."""
    from hermes_state_background import begin
    from hermes_cli.cli_commands_session_tools import _cp
    db = _store(cli, create=True)
    parent = getattr(cli, "session_id", None)
    if not isinstance(parent, str) or not parent:
        raise ValueError("background stored parent identity missing")
    binding = {"profile_id": str(Path(db.db_path).parent.resolve()),
               "parent_session_id": parent, "event_type": event_type,
               "input_sha256": _digest(inputs),
               "config_sha256": _digest({
                   "route": route, "cwd": os.getcwd(), "config": getattr(cli, "config", {}),
                   "options": {name: getattr(cli, name, None) for name in (
                       "max_turns", "enabled_toolsets", "reasoning_config", "service_tier",
                       "_providers_only", "_providers_ignore", "_providers_order", "_provider_sort",
                       "_provider_require_params", "_provider_data_collection",
                       "_openrouter_min_coding_score", "_fallback_model")}})}
    if not db.get_session(parent):
        db.create_session(parent, source="cli")
    record = begin(db, task_id, binding)
    if not record.get("claimed"):
        _cp(f"  {task_id}: {record['status']}; existing task was not restarted")
        display_results(cli)
        return None
    return LocalSideResult(cli, db, task_id, binding)


def display_results(cli):
    """Read the sealed side channel; never add its text to conversation history."""
    from hermes_state_background import results
    from hermes_cli.cli_commands_mixin import _print_side_result_panel
    from hermes_cli.cli_commands_session_tools import _cp
    parent = getattr(cli, "session_id", None)
    if not parent:
        return
    from hermes_constants import get_hermes_home
    viewer_profile = str(get_hermes_home().resolve())
    lock, seen = _display_state(cli)
    try:
        with lock:
            from hermes_cli.gateway_session_store import GatewaySessionStore
            store = getattr(cli, "_session_db", None)
            if isinstance(store, GatewaySessionStore):
                # Use the attach snapshot once before refreshing: opening the target must
                # remain one resume, while a later explicit resume may recover newer results.
                snapshots = cli.__dict__.setdefault("_background_result_snapshots", {})
                cached = store.snapshots.get(parent)
                snapshot = cached if cached is not None and snapshots.get(parent) is not cached else store.call(
                    "session.resume", session_id=parent)
                snapshots[parent] = cached
                if not _gateway_owner(cli, (viewer_profile, parent, None)):
                    return
                profile_id = viewer_profile
                if (snapshot.get("stored_session_id") != parent or
                        snapshot.get("info", {}).get("profile_id") != profile_id):
                    raise ValueError("background result binding mismatch")
                events = snapshot.get("background_results") or []
                identity_scope = profile_id
            else:
                db = _store(cli)
                if db is None:
                    return
                events = results(db, parent)
                identity_scope = str(Path(db.db_path).resolve())
            for event in events:
                if event.get("event_type") not in ("background.complete", "btw.complete"):
                    continue
                payload = event["payload"]
                task_id = payload["task_id"]
                identity = (identity_scope, parent, task_id)
                sealed = not isinstance(store, GatewaySessionStore) or _gateway_completed(store, identity, event)
                displayed = seen if sealed else cli.__dict__.setdefault("_background_result_notices", set())
                if identity in displayed:
                    continue
                pending = cli.__dict__.get("_background_results_pending", {}).get(identity)
                if pending is not None and (not sealed or
                        pending[0].text is not None and not _matches_gateway_result(pending[0], event)):
                    continue
                label = "/btw" if event["event_type"] == "btw.complete" else "/bg"
                if isinstance(store, GatewaySessionStore) and not _gateway_owner(cli, identity):
                    return
                _print_side_result_panel(
                    cli, header_lines=[f"  {label} · {task_id}"], body=payload["text"],
                    title_suffix=f"({label} · {task_id})", empty_note=f"  {task_id}: (empty result)")
                displayed.add(identity)
                if sealed and isinstance(store, GatewaySessionStore):
                    cli.__dict__.setdefault("_background_result_witnesses", {})[identity] = {
                        "event_type": event["event_type"],
                        "payload": {"task_id": task_id, "text": payload["text"]},
                    }
            if isinstance(store, GatewaySessionStore):
                _display_pending_gateway_results(cli, events)
    except Exception as exc:
        if _gateway_owner(cli, (viewer_profile, parent, None)):
            _cp(f"  background results unavailable: {exc}; no task was restarted")
        logger.debug("background result display failed", exc_info=True)


def start_background_worker(cli, task_id, task_num, preview, produce, *, side_result=None):
    from hermes_cli.cli_commands_session_tools import _t

    def done():
        cli._background_tasks.pop(task_id, None)
        if not cli._agent_running:
            cli._spinner_text = ""

    thread = cli._side_worker(
        produce, name=f"bg-task-{task_id}", fail_label=_t("background.label", number=task_num),
        header_lines=[f"  {_t('background.complete', number=task_num)}",
                      f"  {_t('background.prompt', preview=preview)}"],
        title_suffix=_t("background.title_suffix", number=task_num),
        empty_note=f"  {_t('background.no_response')}", bell=True, on_done=done,
        side_result=side_result)
    cli._background_tasks[task_id] = thread
    thread.start()


def _gateway_owner(cli, identity):
    from hermes_constants import get_hermes_home
    profile, parent, _task_id = identity
    if str(get_hermes_home().resolve()) != profile or getattr(cli, "session_id", None) != parent:
        return False
    # The worker keeps its spawn ContextVars; a newly attached viewer store is the
    # foreground binding even if that worker still resolves the old profile home.
    store = getattr(cli, "_session_db", None)
    current = store.__dict__.get("current") if store is not None else None
    return not isinstance(current, dict) or (current.get("stored_session_id") == parent
            and current.get("info", {}).get("profile_id") == profile)


def _matches_gateway_result(result, event):
    from agent.redact import redact_sensitive_text
    return (result.text is not None and event.get("event_type") == result.event_type
            and event.get("payload", {}).get("task_id") == result.background_result[2]
            and event["payload"].get("text") == redact_sensitive_text(result.text))


def _gateway_completed(store, identity, event):
    """The snapshot also contains unknown-execution notices; only the sealed record is an ACK."""
    from hermes_state_background import PREFIX
    profile, parent, task_id = identity
    raw = store.get_meta(PREFIX + hashlib.sha256(task_id.encode()).hexdigest())
    record = json.loads(raw) if raw else {}
    return (record.get("status") == "completed" and record.get("task_id") == task_id
            and record.get("profile_id") == profile and record.get("parent_session_id") == parent
            and record.get("event_type") == event["event_type"] and record.get("payload") == event["payload"])


def _display_pending_gateway_results(cli, events):
    """Retain unconfirmed bodies in this viewer, without claiming durable consumption."""
    from hermes_cli.cli_commands_session_tools import _cp
    pending = cli.__dict__.setdefault("_background_results_pending", {})
    for identity, item in list(pending.items()):
        if not _gateway_owner(cli, identity):
            continue
        result, shown = item
        canonical = next((event for event in events if event.get("event_type") == result.event_type
                          and event.get("payload", {}).get("task_id") == identity[2]), None)
        if canonical is None and result.text is not None:
            canonical = cli.__dict__.get("_background_result_witnesses", {}).get(identity)
        if (identity in cli.__dict__.get("_background_results_shown", set()) and canonical is not None
                and (result.text is None or _matches_gateway_result(result, canonical))):
            pending.pop(identity)
            continue
        if shown:
            continue
        text = result.text if result.text is not None else str(result.error)
        _cp(f"  {identity[2]}: result unconfirmed; preserve this in-memory result before restart.\n{text}")
        pending[identity] = (result, True)


def _display_gateway_result(cli, result):
    """Only canonical results enter seen; every other outcome stays with its original owner."""
    from hermes_cli.gateway_session_store import GatewaySessionStore
    identity = result.background_result
    lock, seen = _display_state(cli)
    with lock:
        already_shown = identity in seen
        pending = cli.__dict__.setdefault("_background_results_pending", {})
        pending[identity] = (result, False)
        if not _gateway_owner(cli, identity):
            return False
        store = getattr(cli, "_session_db", None)
        if not isinstance(store, GatewaySessionStore):
            _display_pending_gateway_results(cli, [])
            return False
        # Always refresh here: the attach snapshot can predate the live terminal event.
        snapshots = cli.__dict__.setdefault("_background_result_snapshots", {})
        snapshots[identity[1]] = store.snapshots.get(identity[1])
        display_results(cli)
        # A failed snapshot read must not hide the only remaining copy of a result.
        _display_pending_gateway_results(cli, [])
        return not already_shown and identity in seen


def side_worker(cli, produce, *, name, fail_label, header_lines, title_suffix, empty_note,
                bell=False, on_done=None, console=None, side_result=None):
    from hermes_cli.cli_commands_mixin import _print_side_result_panel, _refresh_tui_before_print
    from hermes_cli.cli_commands_session_tools import _cp, _t

    def show_failure(exc):
        _refresh_tui_before_print(cli)
        line = f"  {_t('side_result.failed', label=fail_label, error=exc)}"
        if console is not None:
            console.print(line, markup=False)
        else:
            _cp(line)

    def run():
        from hermes_cli.gateway_side import SideResultWait
        gateway_result = isinstance(produce, SideResultWait) and side_result is None
        try:
            body = produce() if side_result is None else side_result.produce(produce)

            def show():
                _print_side_result_panel(cli, header_lines=header_lines, body=body,
                                         title_suffix=title_suffix, empty_note=empty_note,
                                         console=console)
                if bell:
                    cli._ring_bell(context=_t("side_result.complete", label=fail_label))

            if side_result is None:
                if gateway_result:
                    if _display_gateway_result(cli, produce) and bell:
                        cli._ring_bell(context=_t("side_result.complete", label=fail_label))
                else:
                    show()
            else:
                side_result.display(show)
        except Exception as exc:
            logger.debug("side-result worker failed", exc_info=True)
            if gateway_result:
                _display_gateway_result(cli, produce)
            elif side_result is None:
                show_failure(exc)
            elif not side_result.sealed:
                show_failure(RuntimeError(
                    f"{side_result.task_id}: unknown_execution; result was not sealed; do not restart the task"))
            else:
                side_result.display(lambda error=exc: show_failure(error))
        finally:
            if on_done is not None:
                on_done()
            if cli._app:
                cli._invalidate(min_interval=0)

    from agent.memory_provider import spawn_context_thread
    return spawn_context_thread(run, name=name)
