"""Explicit recovery of completed terminal notifications, using native watchers.

No new queue/ledger: the ordinary completion event keeps (id, started_at), and
SessionAuthority's durable completion admission owns restart deduplication. A
repeated scan may offer that same event again; it never creates another job.
"""
import logging
import re
import sqlite3
from tools.process_registry_results import consume_completed_result, iter_completed_results

logger = logging.getLogger("tools.process_registry")


class ProcessResultRecoveryMixin:
    @staticmethod
    def _exit_fields(session):
        return {
            "exit_code": session.exit_code,
            **ProcessResultRecoveryMixin._result_fields(session),
            "completion_reason": session.completion_reason,
            "termination_source": session.termination_source,
        }

    @staticmethod
    def _result_fields(session):
        return {
            "result_durability": session.result_durability,
            "result_verification": session.result_verification,
            **({"result_profile_home": session._result_profile_home} if session._result_profile_home else {}),
            **({"source_fingerprint": session.source_fingerprint} if session.source_fingerprint else {}),
            **({"result_fingerprint": session.result_fingerprint} if session.result_fingerprint else {}),
        }

    @staticmethod
    def _completion_notification(session):
        from tools.process_registry import ProcessRegistry, _completion_output
        return {
            "type": "completion", "session_id": session.id, "task_id": session.task_id,
            "session_key": session.session_key, "owner_task_id": session.owner_task_id,
            "command": session.command,
            **({"handoff_note": session.handoff_note} if session.handoff_note else {}),
            **ProcessRegistry._exit_fields(session), **_completion_output(session),
            "started_at": session.started_at, "parent_session_id": session.parent_session_id,
        }

    def _record_completion_consumed(self, session, *, persist=True):
        """Persist an actual wait/log/kill consumption, never an observational poll."""
        self._completion_consumed.add(session.id)
        if persist:
            consume_completed_result(session)
        else:
            # Kill paths hold session._lock and save after publishing their exit.
            session._result_consumed = True

    def recover_completed_results(self) -> int:
        """Gateway-owner startup only, inside each served profile's runtime scope.

        Only observed exits with a verified receipt and live session lineage may
        resume the original notify intent. Remote commands work if the old owner
        collected their exit; a remote/live PID checkpoint alone is insufficient.
        """
        from hermes_state import SessionDB
        sessions = [s for s in iter_completed_results()
                    if s.result_verification == "verified" and s.notify_on_complete
                    and not s._result_consumed and s.exit_code is not None
                    and s.parent_session_id and s.session_key
                    and re.fullmatch(r"proc_[0-9a-f]{12}", s.id)]
        if not sessions:
            return 0
        try:
            db = SessionDB(read_only=True)
        except (OSError, sqlite3.Error):
            logger.warning("Cannot verify completed terminal owners; leaving receipts pending", exc_info=True)
            return 0
        restored = 0
        try:
            for session in sessions:
                # Do not adopt an orphan or an ended/replaced route as a new owner.
                if db.get_session(session.parent_session_id) is None:
                    continue
                tip = db.get_compression_tip(session.parent_session_id)
                if not tip or db.get_session(tip) is None:
                    continue
                with self._lock:
                    if session.id in self._running:
                        continue  # A live/re-adopted incarnation wins; never fabricate an exit.
                    existing = self._finished.get(session.id)
                    if existing is not None:
                        if existing.source_fingerprint != session.source_fingerprint:
                            continue
                        if existing._result_recovered:
                            continue  # Same receipt on a repeated startup scan.
                    if session.id in self._completion_consumed:
                        continue
                    self._finished[session.id] = session
                event = {**self._completion_notification(session), "restored": True}
                self.completion_queue.put(event)
                if session.watcher_platform:
                    self.pending_watchers.append({
                        "session_id": session.id, "check_interval": session.watcher_interval or 1,
                        "session_key": session.session_key, "platform": session.watcher_platform,
                        "chat_id": session.watcher_chat_id, "user_id": session.watcher_user_id,
                        "user_name": session.watcher_user_name, "thread_id": session.watcher_thread_id,
                        "message_id": session.watcher_message_id,
                        "notify_on_complete": True, "parent_session_id": session.parent_session_id,
                    })
                restored += 1
        except (OSError, sqlite3.Error):
            logger.warning("Could not verify completed terminal lineage", exc_info=True)
        finally:
            db.close()
        return restored
