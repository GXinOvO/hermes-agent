"""The bound session row's durable compressor state for ``agent.context_compressor``.

Best-effort reads and writes of the per-session values a rebuilt compressor resumes from (streaks, strike counts,
deadlines, model-config keys, the failure cooldown), through the store's interface handle when it has one, else the
store's own methods. ``ContextCompressor`` takes ``DurableStateMixin`` as a base.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

from persistence.core.errors import PersistenceError

# Log-record parity with the origin module.
logger = logging.getLogger("agent.context_compressor")


# The storage interface verb each durable per-session read or write runs on a writer handle: (repository, verb).
_DURABLE_VERBS = {
    "get_compression_fallback_streak": ("lineage", "compression_fallback_streak"),
    "get_compression_ineffective_count": ("lineage", "compression_ineffective_count"),
    "get_compression_overload_streak": ("lineage", "compression_overload_streak"),
    "get_compression_recovery_deadline": ("lineage", "compression_recovery_deadline"),
    "increment_compression_overload_streak": ("lineage", "increment_compression_overload_streak"),
    "set_compression_fallback_streak": ("lineage", "set_compression_fallback_streak"),
    "set_compression_ineffective_count": ("lineage", "set_compression_ineffective_count"),
    "set_compression_overload_streak": ("lineage", "set_compression_overload_streak"),
    "set_compression_recovery_deadline": ("lineage", "set_compression_recovery_deadline"),
    "get_session_model_config_value": ("sessions", "get_model_config_value"),
    "patch_session_model_config": ("sessions", "patch_model_config"),
    "record_compression_failure_cooldown": ("messages", "record_compression_cooldown"),
    "clear_compression_failure_cooldown": ("messages", "clear_compression_cooldown"),
}


def _durable_verb(store: Any, session_db: Any, method: str) -> Any:
    """What a durable read or write named ``method`` calls: the interface verb on ``store`` (a writer handle), else
    ``session_db``'s own method (None when it has none)."""
    if store is not None:
        repository, verb = _DURABLE_VERBS[method]
        return getattr(getattr(store, repository), verb)
    return getattr(session_db, method, None)


class DurableStateMixin:
    """``ContextCompressor`` mixin: best-effort reads and writes of the bound row's durable state."""

    def _store_interface(self) -> Any:
        """The bound store's storage interface handle when the compressor can reach one, else None.
        A stand-in ``hermes_state.SessionDB`` carries no durable compression verbs, so it must resolve to
        the writer handle it forwards to; without it every durable cooldown read fails and compression
        aborts as ``cooldown_state_unreadable`` on a healthy store."""
        from agent.agent_init_session import stand_in_interface, writer_interface
        session_db = getattr(self, "_session_db", None)
        return (writer_interface(getattr(self, "_persistence", None), session_db)
                or stand_in_interface(session_db))

    def _durable_read(
        self, method: str, label: str, coerce, default, *args,
        session_db: Any = None, session_id: Optional[str] = None, persistence: Any = None,
    ):
        """Best-effort read of a durable per-session value; ``default`` when unbound/unsupported/failed.
        Returns ``(found, value)``: ``found`` is False when no read happened; ``value`` is None when the
        row held a non-numeric value. Defaults to the bound session row; pass
        ``session_db``/``session_id`` (and the handle beside that store) to read another row (parent lineage)."""
        if session_db is None:
            session_db, persistence = getattr(self, "_session_db", None), getattr(self, "_persistence", None)
        session_id = getattr(self, "_session_id", "") if session_id is None else session_id
        from agent.agent_init_session import writer_interface
        getter = _durable_verb(writer_interface(persistence, session_db), session_db, method)
        if not session_id or not callable(getter):
            return False, default
        try:
            stored = getter(session_id, *args)
            if isinstance(stored, (int, float, str)):
                return True, max(default, coerce(stored))
            return True, None
        except Exception as exc:  # health: allow BLE001 -- a durable read is best-effort: any failure reads as no value
            suffix = "" if isinstance(exc, (TypeError, ValueError, sqlite3.Error, PersistenceError)) else " (non-sqlite)"
            logger.debug("%s lookup failed%s: %s", label, suffix, exc)
        return False, default

    def _durable_write(self, method: str, label: str, *args) -> bool:
        """Best-effort write of a durable per-session value; True only when the write succeeded."""
        # Class-qualified: tests bind this single method onto a bare stub.
        setter = _durable_verb(DurableStateMixin._store_interface(self), getattr(self, "_session_db", None), method)
        if not getattr(self, "_session_id", "") or not callable(setter):
            return False
        session_id = self._session_id
        try:
            setter(session_id, *args)
            return True
        except Exception as exc:  # health: allow BLE001 -- a durable write is best-effort: any failure reports False
            suffix = "" if isinstance(exc, (sqlite3.Error, PersistenceError)) else " (non-sqlite)"
            logger.debug("%s persist failed%s: %s", label, suffix, exc)
        return False

    def _load_durable(self, attr: str, method: str, label: str, coerce, default, *args) -> None:
        """Restore ``self.<attr>`` from the bound row; a non-numeric row resets it to ``default``."""
        found, value = self._durable_read(method, label, coerce, default, *args)
        if found:
            setattr(self, attr, default if value is None else value)

    def _load_fallback_compression_streak(self) -> None:
        self._load_durable("_fallback_compression_streak", "get_compression_fallback_streak", "compression fallback streak", int, 0)

    def _carry_stand_in_cooldown_reads(self, session_db: Any) -> None:
        """Give a deprecated ``hermes_state.SessionDB`` the cooldown reads its legacy callers still make.
        The merged stand-in carries only the plugin guide's subset, so a legacy reader (and this fork's
        compression tests) got ``AttributeError`` where the fork's own ``SessionDB`` answered from the same
        row. The reads forward to the handle the stand-in already holds; nothing is cached, and a store
        without them stays silent. The writes need no bridge: the compressor writes through that handle."""
        from agent.agent_init_session import stand_in_interface
        store = stand_in_interface(session_db)
        if store is None:
            return

        def get_compression_failure_cooldown(session_id: str) -> Optional[dict[str, Any]]:
            now = time.time()
            cooldown = store.messages.read_compression_cooldown(session_id, now)
            return None if cooldown is None else {
                "cooldown_until": cooldown.until, "remaining_seconds": cooldown.until - now, "error": cooldown.error}

        def get_compression_failure_cooldown_row(session_id: str) -> dict[str, Any]:
            snapshot = store.messages.read_compression_cooldown_snapshot(session_id)
            return {"session_exists": snapshot.present, "cooldown_until": snapshot.until, "error": snapshot.error}

        for name, reader in (
            ("get_compression_failure_cooldown", get_compression_failure_cooldown),
            ("get_compression_failure_cooldown_row", get_compression_failure_cooldown_row),
        ):
            # An instance attribute shadows the stand-in's raising __getattr__; a store that answers the
            # name itself keeps its own method.
            if not hasattr(type(session_db), name):
                setattr(session_db, name, reader)
