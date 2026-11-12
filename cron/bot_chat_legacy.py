"""Deferred sealed Bot results; old unsealed records remain explicit unknown evidence.

A queued sealed result may reach its owner once. Claimed/lost-ACK records are never
resent. Records remain as evidence of their final disposition.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)
_warned: set[Path] = set()


def defer(key: str, job: dict, content: str, home: Path, *, for_failure: bool) -> None:
    """Park one never-admitted output under its delivery id; a later tick hands it to the owner."""
    import time
    from cron.sealed_bot_result import validate_defer
    validate_defer(key, job, content, home, for_failure)
    root = get_hermes_home().resolve() / "cron" / "bot_chat_pending"
    make_user_dir(root, parents=True, exist_ok=True, mode=0o700)
    path = root / f"{key}.json"
    if not path.exists():  # an existing record (queued or already transferred) keeps its status
        atomic_json_write(path, dict(
            id=key, status="queued", job=dict(job), content=content,
            home=str(home), for_failure=for_failure, sequence=time.time_ns()), fsync_dir=True, mode=0o600)


def pending_status(key: str, job: dict, content: str, home: Path, *, for_failure: bool):
    path = get_hermes_home().resolve() / 'cron' / 'bot_chat_pending' / f'{key}.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    saved = record.get('job') if isinstance(record, dict) else None
    if (not isinstance(saved, dict) or record.get('id') != key or record.get('home') != str(home)
            or record.get('for_failure') is not for_failure
            or saved.get('id') != job.get('id') or saved.get('execution_id') != job.get('execution_id')
            or saved.get('_sealed_bot_result_v1') != job.get('_sealed_bot_result_v1')):
        raise ValueError('deferred Bot delivery identity mismatch')
    if record.get('content') != content:
        # Two notices composed for one execution share a delivery identity, and the parked
        # record still owns it (it is never handed off twice): its status decides, not this text.
        if path not in _warned:
            _warned.add(path)
            logger.warning('Parked Bot Chat record %s holds different text for delivery %s; '
                           'reporting its status %r', path, key, record.get('status'))
    status = record.get('status') or 'unknown'
    if status == 'claimed':
        pid, started = record.get('owner_pid'), record.get('owner_started_at')
        if type(pid) is not int or pid <= 0:
            # Only a release that predates the owner stamp writes ``claimed`` without one, so this
            # consumer is gone: settle as unknown rather than park the queue row forever. Either
            # disposition keeps the at-most-once fence — the record is never handed off twice.
            return 'ambiguous'
        from cron.executions import _owner_is_live
        from gateway.status import _pid_exists
        if started is None:
            try:
                live = _pid_exists(pid)
            except Exception:
                live = True
        elif type(started) is int and started > 0:
            live = _owner_is_live(pid, started)
        else:
            live = True
        if not live:
            return 'ambiguous'
    return status


def _queued(root: Path) -> list[tuple[Path, dict]]:
    found = []
    for path in root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError(f"expected a JSON object, got {type(record).__name__}")
        except (OSError, ValueError) as exc:
            if path not in _warned:
                _warned.add(path)
                logger.error("Unreadable legacy Bot Chat pending record %s kept as evidence: %s", path, exc)
            continue
        if record.get("status") == "queued":
            found.append((path, record))
    return sorted(found, key=lambda item: (item[1].get("sequence", 0), item[0].name))


def _malformed(record: dict) -> str:
    """Why *record* can never be handed off (empty when its required fields are usable)."""
    missing = [key for key in ("id", "home", "content")
               if not isinstance(record.get(key), str) or not record.get(key)]
    if missing:
        return f"malformed legacy bot-chat record (missing {', '.join(missing)}); not delivered"
    if not isinstance(record.get("job") or {}, dict):
        return "malformed legacy bot-chat record (job is not an object); not delivered"
    return ""


def _dispose(path: Path, record: dict) -> bool:
    """Decide one queued record in place; False leaves it queued for a later tick."""
    from cron.scheduler_delivery import BOT_CHAT_POLICY_PLATFORM, bot_chat_message
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from tools.bot_live_delivery import deliver_to_live_owner, find_canonical_live_owner

    if reason := _malformed(record):
        record.update(status="ambiguous", error=reason)
        return True
    if not (record.get('job') or {}).get('_sealed_bot_result_v1'):
        record.update(status='ambiguous', error='legacy result provenance unknown; not consumed again')
        return True
    from cron.sealed_bot_result import canonical_target_home
    try:
        home = canonical_target_home(record["home"])
    except ValueError as exc:
        record.update(status='ambiguous', error=str(exc))
        return True
    for_failure = bool(record.get("for_failure"))
    if not (home / "state.db").is_file():
        record.update(status="ambiguous", error=f"bot-chat delivery target no longer exists: {home}")
    elif for_failure and not warning_notifications_enabled(
            BOT_CHAT_POLICY_PLATFORM, load_user_config_effective(home / "config.yaml")):
        record.update(status="suppressed", error=None)
    else:
        try:
            owner = find_canonical_live_owner(home)
        except ValueError:
            return False  # Owner not ready: discovery uncertainty is not a disposition.
        if owner is None:
            return False
        try:
            from cron.sealed_bot_result import producer_reference
            from cron.executions import _process_start_time
            sealed = producer_reference(record['job'], record['content'], home, owner)
            # Persist intent before the native door; an interrupted consumer stays unknown.
            pid = os.getpid()
            record.update(status='claimed', owner_pid=pid, owner_started_at=_process_start_time(pid))
            atomic_json_write(path, record, fsync_dir=True, mode=0o600)
            receipt = deliver_to_live_owner(
                home, owner, bot_chat_message(record.get("job") or {}, record["content"]),
                delivery_id=record["id"], notification_category="diagnostic" if for_failure else "result",
                sealed_cron_result_v1=sealed)
        except Exception as exc:
            record.update(status='ambiguous', error=f'consumer admission unknown: {exc}; do not replay')
            logger.log(logging.DEBUG if path in _warned else logging.WARNING,
                       "Legacy Bot Chat pending record %s not handed off yet: %s", path, exc, exc_info=True)
            _warned.add(path)
            return True
        receipt_status = receipt.get("status") if isinstance(receipt, dict) else None
        record.update(status="transferred", error=None, receipt_status=receipt_status)
    return True


def drain_legacy_pending() -> None:
    """Admit queued legacy records to their live owner; an absent owner leaves them for a later tick.

    Each record is isolated: a malformed one is kept as ``ambiguous`` evidence and an
    unexpected failure leaves only that record queued, so one bad file never stops cron.
    """
    root = get_hermes_home().resolve() / "cron" / "bot_chat_pending"
    if not root.is_dir():
        return
    for path, record in _queued(root):
        try:
            if not _dispose(path, record):
                continue
            atomic_json_write(path, record, fsync_dir=True, mode=0o600)
        except Exception as exc:
            logger.log(logging.DEBUG if path in _warned else logging.WARNING,
                       "Legacy Bot Chat pending record %s left queued: %s", path, exc, exc_info=True)
            _warned.add(path)
            continue
        logger.info("Legacy Bot Chat pending record %s -> %s", path.name, record["status"])
