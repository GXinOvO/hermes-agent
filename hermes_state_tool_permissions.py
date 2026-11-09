"""Session tool authorization, independent of the disposable tools[] cache.

The reserved model_config sidecar is written together with the pin on the native
transaction connection. Legacy readable pins are migrated lazily. An absent pin
is legacy-unrestricted only before an explicit restriction has been recorded.
"""
import json

PERMISSION_KEY = "_tool_permissions"


class SessionToolPermissionMixin:
    """SessionDB write entry points for the durable tool-permission sidecar."""

    def update_session_meta(self, session_id, model_config_json, model=None):
        from hermes_state_sessions import _parse_model_config
        self.flush_token_counts()

        def _do(conn):
            row = conn.execute("SELECT model_config FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return
            config = preserve_tool_permission(row[0], _parse_model_config(model_config_json))
            conn.execute("UPDATE sessions SET model_config=?, model=COALESCE(?,model) WHERE id=?",
                         (json.dumps(config), model, session_id))

        self._execute_write(_do)

    def update_session_tool_names(self, session_id, pin, *, preserve_cache=False):
        self._execute_write(lambda conn: write_tool_pin(self, conn, session_id, pin, preserve_cache=preserve_cache))


def _config(raw):
    if raw is None:
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("Invalid persisted tool permission metadata")
    return dict(value)


def _allowed(value):
    from tools.delegate_tool_allowlist import _validate_value
    if error := _validate_value(0, value):
        raise ValueError(error)
    return list(value)


def saved_tool_permission(model_config, raw_pin):
    """Return the durable allowance; None means proven/legacy unrestricted.

    An unreadable pin without independent metadata cannot prove unrestricted.
    Known metadata survives missing or swept content-addressed cache entries.
    """
    config = _config(model_config)
    known = PERMISSION_KEY in config
    allowed = None
    if known:
        meta = config[PERMISSION_KEY]
        if not isinstance(meta, dict) or set(meta) != {"version", "allowed_tools"} or type(meta["version"]) is not int or meta["version"] != 1:
            raise ValueError("Invalid persisted tool permission metadata")
        allowed = None if meta["allowed_tools"] is None else _allowed(meta["allowed_tools"])
    try:
        pin = json.loads(raw_pin) if raw_pin is not None else None
    except (ValueError, TypeError) as exc:
        if not known:
            raise ValueError("Cannot recover tool permission from unreadable pin") from exc
        return allowed
    if pin is not None and not isinstance(pin, (dict, list)):
        raise ValueError("Cannot recover tool permission from invalid pin")
    if isinstance(pin, dict) and "allowed_tools" in pin:
        old = _allowed(pin["allowed_tools"])
        return old if allowed is None else [name for name in allowed if name in old]
    if raw_pin is not None and not known and not usable_tool_cache(pin):
        raise ValueError("Cannot recover tool permission from invalid pin")
    return allowed


def write_tool_pin(db, conn, session_id, pin, *, preserve_cache=False):
    """Compare/read/write on the caller's transaction; never commit here."""
    row = conn.execute(
        "SELECT s.model_config, COALESCE(p.prompt,s.tool_names) FROM sessions s "
        "LEFT JOIN system_prompts p ON p.hash=s.tool_names WHERE s.id=?", (session_id,),
    ).fetchone()
    if row is None:
        if preserve_cache or (isinstance(pin, dict) and "allowed_tools" in pin):
            raise ValueError("Cannot persist tool permission: session row is missing")
        return
    current = saved_tool_permission(row[0], row[1])
    incoming = _allowed(pin["allowed_tools"]) if isinstance(pin, dict) and "allowed_tools" in pin else None
    if current is not None and incoming is not None and not set(incoming) <= set(current):
        raise ValueError("Tool permission cannot expand an existing allowed_tools restriction")
    if preserve_cache and incoming is None:
        raise ValueError("Cache-preserving persistence requires explicit allowed_tools")
    allowed = incoming if incoming is not None else current
    config = _config(row[0])
    config[PERMISSION_KEY] = {"version": 1, "allowed_tools": allowed}
    if preserve_cache:
        conn.execute("UPDATE sessions SET model_config=? WHERE id=?", (json.dumps(config), session_id))
        return
    encoded = json.dumps(pin) if pin is not None else None
    conn.execute("UPDATE sessions SET tool_names=?, model_config=? WHERE id=?",
                 (db._store_system_prompt(conn, encoded), json.dumps(config), session_id))
    db._delete_unreferenced_system_prompts(conn)


def write_tool_permission_sidecar(db, session_id, allowed):
    """Commit a narrowed allowance without replacing an unreadable tools cache."""
    db.update_session_tool_names(
        session_id, {"version": "permission-only", "tools": [], "allowed_tools": _allowed(allowed)},
        preserve_cache=True,
    )


def restore_session_tool_permission(agent, session_row):
    """Restore authorization before attempting any cache/prompt recovery."""
    from tools.delegate_tool_allowlist import restore_saved_restriction
    if session_row is None:
        return None
    try:
        allowed = saved_tool_permission(session_row.get("model_config"), session_row.get("tool_names"))
    except ValueError:
        agent.allowed_tools, agent.tools, agent.valid_tool_names = [], [], set()
        agent._tool_pin_unusable = True
        agent._tool_authority_unknown = True
        raise
    if allowed is not None:
        restore_saved_restriction(agent, {"allowed_tools": allowed})
    return allowed


def usable_tool_cache(pin):
    if isinstance(pin, list):
        return all(isinstance(name, str) for name in pin)
    if not isinstance(pin, dict) or not isinstance(pin.get("version"), str) or not isinstance(pin.get("tools"), list):
        return False
    return all(isinstance(tool, dict) and isinstance(tool.get("function"), dict)
               and isinstance(tool["function"].get("name"), str) for tool in pin["tools"])


def preserve_tool_permission(current, replacement):
    """Ordinary model-config writers cannot create/change/remove this sidecar."""
    config = _config(current)
    if PERMISSION_KEY in replacement and (
            PERMISSION_KEY not in config or replacement[PERMISSION_KEY] != config[PERMISSION_KEY]):
        raise ValueError("Tool permission metadata is reserved for session.tools")
    result = dict(replacement)
    if PERMISSION_KEY in config:
        result[PERMISSION_KEY] = config[PERMISSION_KEY]
    return result


def inherited_tool_config(conn, parent_id, config):
    """Carry authorization with the parent's tool pin on native child publication."""
    row = conn.execute(
        "SELECT s.model_config, COALESCE(p.prompt,s.tool_names) FROM sessions s "
        "LEFT JOIN system_prompts p ON p.hash=s.tool_names WHERE s.id=?", (parent_id,),
    ).fetchone()
    if row is None:
        return config
    allowed = saved_tool_permission(row[0], row[1])
    parent_config = _config(row[0])
    result = dict(config or {})
    if allowed is not None or PERMISSION_KEY in parent_config:
        requested = result.pop(PERMISSION_KEY, None)
        if requested is not None:
            if not isinstance(requested, dict) or requested.get("version") != 1:
                raise ValueError("Inherited tool permission metadata is reserved")
            child_allowed = requested.get("allowed_tools")
            child_allowed = None if child_allowed is None else _allowed(child_allowed)
            if allowed is not None and (child_allowed is None or not set(child_allowed) <= set(allowed)):
                raise ValueError("Inherited tool permission cannot expand parent allowed_tools")
            allowed = child_allowed if child_allowed is not None else allowed
        result[PERMISSION_KEY] = {"version": 1, "allowed_tools": allowed}
    return result


def _permission_state(raw, raw_pin=None):
    if isinstance(raw, tuple) and len(raw) == 2 and raw_pin is None:
        raw, raw_pin = raw
    config = _config(raw)
    allowed = saved_tool_permission(raw, raw_pin)
    return PERMISSION_KEY in config or allowed is not None, allowed


def prepare_child_model_config(conn, parent_id, existing_raw, incoming):
    """Validate child permission metadata and add inherited authority only with constructor facts."""
    result = _config(incoming)
    parent = conn.execute(
        "SELECT s.model_config, COALESCE(p.prompt,s.tool_names) FROM sessions s "
        "LEFT JOIN system_prompts p ON p.hash=s.tool_names WHERE s.id=?", (parent_id,),
    ).fetchone()
    if parent is None:
        return result
    parent_known, parent_allowed = _permission_state(parent[0], parent[1])
    existing_known, existing_allowed = _permission_state(existing_raw)
    if PERMISSION_KEY in result:
        requested = result[PERMISSION_KEY]
        if not isinstance(requested, dict) or set(requested) != {"version", "allowed_tools"} or requested.get("version") != 1:
            raise ValueError("Inherited tool permission metadata is reserved")
        requested_allowed = None if requested["allowed_tools"] is None else _allowed(requested["allowed_tools"])
        if parent_known and parent_allowed is not None and (requested_allowed is None or not set(requested_allowed) <= set(parent_allowed)):
            raise ValueError("Inherited tool permission cannot expand parent allowed_tools")
        if existing_known and existing_allowed is not None and (requested_allowed is None or not set(requested_allowed) <= set(existing_allowed)):
            raise ValueError("Inherited tool permission cannot expand existing allowed_tools")
        result[PERMISSION_KEY] = {"version": 1, "allowed_tools": requested_allowed}
    elif parent_known or existing_known:
        inherited = parent_allowed
        if existing_known and existing_allowed is not None:
            inherited = existing_allowed if inherited is None else [name for name in existing_allowed if name in inherited]
        result[PERMISSION_KEY] = {"version": 1, "allowed_tools": inherited}
    return result


def merge_model_config(existing, incoming, *, allow_permission=False):
    """Merge constructor fields without replacing an authoritative permission sidecar."""
    current = _config(existing)
    proposed = _config(incoming)
    if PERMISSION_KEY in proposed:
        if not allow_permission:
            proposed = preserve_tool_permission(existing, proposed)
        else:
            requested = proposed[PERMISSION_KEY]
            if not isinstance(requested, dict) or set(requested) != {"version", "allowed_tools"} or requested.get("version") != 1:
                raise ValueError("Inherited tool permission metadata is reserved")
            requested_allowed = None if requested["allowed_tools"] is None else _allowed(requested["allowed_tools"])
            existing_known, existing_allowed = _permission_state(existing)
            if existing_known and existing_allowed is not None and (requested_allowed is None or not set(requested_allowed) <= set(existing_allowed)):
                raise ValueError("Inherited tool permission cannot expand existing allowed_tools")
            proposed[PERMISSION_KEY] = {"version": 1, "allowed_tools": requested_allowed}
    elif PERMISSION_KEY in current:
        proposed[PERMISSION_KEY] = current[PERMISSION_KEY]
    merged = {**proposed, **current}
    if PERMISSION_KEY in proposed:
        merged[PERMISSION_KEY] = proposed[PERMISSION_KEY]
    return merged
