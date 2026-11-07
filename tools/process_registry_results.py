"""Profile-local receipts for completed terminal processes.

Queries only load snapshots. Explicit gateway recovery may resume the original
notification intent through its existing watcher/admission path, never a PID.
Retention is seven days; only consumed history is capped at 64 receipts. Output
is a redacted bounded tail, not a permanent archive or an external-send ledger.
"""

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("tools.process_registry")

RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETAINED_RESULTS = 64
_RESULT_FIELDS = (
    "id", "command", "cwd", "task_id", "owner_task_id", "session_key",
    "parent_session_id", "started_at", "exit_code", "completion_reason",
    "termination_source", "notify_on_complete",
)
_ROUTING_FIELDS = (
    "watcher_platform", "watcher_chat_id", "watcher_user_id", "watcher_user_name",
    "watcher_thread_id", "watcher_message_id", "watcher_interval", "completion_output_chars",
)
_SOURCE_FIELDS = (
    "receipt_version", "profile_home", "id", "command", "cwd", "task_id", "owner_task_id",
    "session_key", "parent_session_id", "started_at", "notify_on_complete", *_ROUTING_FIELDS,
)
_COMPLETED_FIELDS = (
    "exit_code", "completion_reason", "termination_source", "exited_at", "output",
    "output_chars", "output_truncated",
)


def _fingerprint(record, fields):
    payload = json.dumps({key: record[key] for key in fields}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _consumed_history(path):
    try:
        validated = _validated_record(path)
        return validated is not None and validated[0].get("completion_consumed") is True
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False  # An unreadable receipt is not proof of consumption.


def _result_paths():
    """Apply TTL to all receipts, and the history cap only to consumed results."""
    directory = get_hermes_home() / "logs" / "process-results"
    cutoff = time.time() - RESULT_RETENTION_SECONDS
    retained = []
    for path in directory.glob("proc_*.json"):
        try:
            modified = path.stat().st_mtime
            if modified < cutoff:
                path.unlink(missing_ok=True)
            else:
                retained.append((modified, path))
        except FileNotFoundError:
            continue  # Another producer pruned it.
    retained.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    paths, consumed = [], 0
    for _, path in retained:
        if _consumed_history(path):
            consumed += 1
            if consumed > MAX_RETAINED_RESULTS:
                path.unlink(missing_ok=True)
                continue
        paths.append(path)
    return paths


def save_completed_result(session) -> str:
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools.process_registry import MAX_OUTPUT_CHARS

    # A concurrent wait/log consumption must not be overwritten by the initial save.
    with session._result_persist_lock:
        try:
            with session._lock:
                record = {key: getattr(session, key) for key in (*_RESULT_FIELDS, *_ROUTING_FIELDS)}
                record.update(
                    receipt_version=1, profile_home=str(get_hermes_home().resolve()),
                    exited_at=session.exited_at, output=session.output_buffer[-MAX_OUTPUT_CHARS:],
                    output_chars=max(session.total_output_chars, len(session.output_buffer)),
                    output_truncated=max(session.total_output_chars, len(session.output_buffer)) > MAX_OUTPUT_CHARS,
                    completion_consumed=session._result_consumed,
                )
            # Live-output opt-out must not persist raw credentials in durable receipts.
            record["output"] = redact_terminal_output(record["output"], record["command"], force=True)
            record["command"] = redact_sensitive_text(record["command"], code_file=True, force=True)
            record["source_fingerprint"] = _fingerprint(record, _SOURCE_FIELDS)
            record["result_fingerprint"] = _fingerprint(record, ("source_fingerprint", *_COMPLETED_FIELDS))
            record["consumption_fingerprint"] = _fingerprint(record, ("result_fingerprint", "completion_consumed"))
            directory = get_hermes_home() / "logs" / "process-results"
            from hermes_constants import assert_named_profile_home_live
            assert_named_profile_home_live(directory)
            make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
            atomic_json_write(directory / f"{session.id}.json", record, mode=0o600, fsync_dir=True)
            session._result_profile_home = record["profile_home"]
            session.source_fingerprint = record["source_fingerprint"]
            session.result_fingerprint = record["result_fingerprint"]
            session.result_durability = "persisted"
            session.result_verification = "verified"
        except (OSError, ValueError, TypeError):
            session.result_durability = "failed"
            logger.warning("Could not retain completed process result %s", session.id, exc_info=True)
            return session.result_durability
        try:
            _result_paths()
        except OSError:
            # Pruning failure does not negate the receipt write that already succeeded.
            logger.warning("Could not prune completed process results", exc_info=True)
        return session.result_durability


def _owns_result(owner: str, parent: str | None) -> bool:
    if not parent:
        return False
    if owner == parent:
        return True
    from hermes_state import SessionDB

    # A pure lineage read: queries never create a writer or send a notification.
    db = SessionDB(read_only=True)
    try:
        return db.get_compression_tip(parent) == owner
    finally:
        db.close()


def consume_completed_result(session) -> None:
    """Change only the consumption marker; producer/result fingerprints stay immutable.

    Serialize with the initial result write: an early wait either precedes its
    snapshot or updates that exact receipt after the write, never loses its ACK.
    """
    with session._result_persist_lock:
        session._result_consumed = True
        if session.result_verification != "verified":
            return
        try:
            path = get_hermes_home() / "logs" / "process-results" / f"{session.id}.json"
            validated = _validated_record(path)
            if validated is None:
                raise ValueError("completed result no longer matches its receipt")
            record, verification = validated
            if verification != "verified" or record["result_fingerprint"] != session.result_fingerprint:
                raise ValueError("completed result identity changed before consumption")
            record["completion_consumed"] = True
            record["consumption_fingerprint"] = _fingerprint(record, ("result_fingerprint", "completion_consumed"))
            atomic_json_write(path, record, mode=0o600, fsync_dir=True)
            session.result_durability = "persisted"
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            session.result_durability = "failed"
            logger.warning("Could not retain process result consumption %s", session.id, exc_info=True)


def restored_event_owned(evt, *, checked_owner=False) -> bool:
    """A restored process receipt is never taken by another profile or a reused CLI route."""
    if not evt.get("restored") or evt.get("type") != "completion":
        return True
    if evt.get("result_profile_home") != str(get_hermes_home().resolve()):
        return False
    if checked_owner:
        return True  # the caller's lineage-aware callback still has to accept it
    from gateway.session_context import get_session_env
    owner = get_session_env("HERMES_SESSION_ID", "")
    try:
        return bool(owner) and _owns_result(owner, evt.get("parent_session_id"))
    except (OSError, ValueError, sqlite3.Error):
        return False


def _validated_record(path):
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if record["id"] != path.stem or not re.fullmatch(r"proc_[\w]+", record["id"]):
        return None
    if "receipt_version" not in record:
        return record, "legacy_unverified"
    if record["receipt_version"] != 1 or record["profile_home"] != str(get_hermes_home().resolve()):
        return None
    if not isinstance(record["notify_on_complete"], bool) or not isinstance(record["completion_consumed"], bool):
        return None
    if not isinstance(record["started_at"], (int, float)) or not math.isfinite(record["started_at"]) or record["started_at"] <= 0:
        return None
    if record["source_fingerprint"] != _fingerprint(record, _SOURCE_FIELDS):
        return None
    if record["result_fingerprint"] != _fingerprint(record, ("source_fingerprint", *_COMPLETED_FIELDS)):
        return None
    if record["consumption_fingerprint"] != _fingerprint(record, ("result_fingerprint", "completion_consumed")):
        return None
    return record, "verified"


def iter_completed_results(prefix=""):
    """Validate profile and immutable producer/result binding; no queue writes."""
    from tools.process_registry import ProcessSession

    try:
        paths = _result_paths()
    except OSError:
        logger.warning("Could not read retained process results", exc_info=True)
        return
    for path in paths:
        if not path.stem.startswith(prefix):
            continue
        try:
            validated = _validated_record(path)
            if validated is None:
                continue
            record, verification = validated
            session = ProcessSession(
                **{key: record[key] for key in _RESULT_FIELDS},
                **{key: record[key] for key in _ROUTING_FIELDS if key in record},
                exited=True, output_buffer=record["output"], exited_at=record.get("exited_at", 0),
                total_output_chars=record.get("output_chars", len(record["output"])),
                result_durability="persisted", result_verification=verification,
                source_fingerprint=record.get("source_fingerprint", ""),
                result_fingerprint=record.get("result_fingerprint", ""),
                _result_profile_home=str(get_hermes_home().resolve()), _result_recovered=True,
                _result_consumed=record.get("completion_consumed", False),
            )
            session._completion_event.set()
            yield session
        except (OSError, ValueError, KeyError, TypeError, OverflowError, sqlite3.Error):
            logger.debug("Skipping unreadable process result %s", path.name, exc_info=True)


def result_visible(session) -> bool:
    """Only recovered snapshots require scope checks; live process semantics stay unchanged."""
    if not session._result_recovered:
        return True
    if session._result_profile_home != str(get_hermes_home().resolve()):
        return False
    from gateway.session_context import get_session_env
    owner = get_session_env("HERMES_SESSION_ID", "")
    try:
        return bool(owner) and _owns_result(owner, session.parent_session_id)
    except (OSError, ValueError, sqlite3.Error):
        return False


def load_completed_results(prefix: str = "") -> dict:
    """Read-only snapshots; no process handles, watchers, or queue events."""
    from gateway.session_context import get_session_env
    if not get_session_env("HERMES_SESSION_ID", ""):
        return {}
    return {session.id: session for session in iter_completed_results(prefix) if result_visible(session)}
