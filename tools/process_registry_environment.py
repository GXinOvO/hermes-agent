"""Backend-owned background process lifecycle; ordinary remote shell fallback."""
from __future__ import annotations

import codecs
import shlex
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


class ProcessEnvironmentMixin:
    def spawn_via_env(
        self: Any, env: Any, command: str, cwd: str | None = None, task_id: str = "", session_key: str = "",
        timeout: int = 10, owner_task_id: str = "", persist_on_release: bool = False) -> ProcessSession:
        """Spawn a background process inside a non-local backend's sandbox.
        The command is wrapped to capture its in-sandbox PID and redirect output to a
        log file that later execute() calls poll. No live pipe or stdin, but it runs in
        the correct sandbox context. ``persist_on_release`` keeps the process out of
        agent-lifecycle kill sweeps (#41225)."""
        session = self._new_session(command, task_id, owner_task_id, session_key, cwd, env_ref=env, pid_scope="sandbox",
                                    persist_on_release=persist_on_release)
        if callable(getattr(env, 'start_owned_process', None)):
            handle = env.start_owned_process(command, cwd=cwd, lifetime='background')
            session.owned_handle = handle
            session.owned_generation = handle.identifier
            session.pid_scope = 'owned'
            try:
                self._track_started(session, self._owned_reader_loop, f'proc-owned-reader-{session.id}')
            except BaseException:
                env.signal_owned(handle)
                raise
            return session
        temp_dir = self._env_temp_dir(env)
        log_path, pid_path, exit_path = (f"{temp_dir}/hermes_bg_{session.id}.{ext}" for ext in ("log", "pid", "exit"))
        q = shlex.quote
        bg_command = (
            f"mkdir -p {q(temp_dir)} && "
            f"( nohup bash -lc {q(command)} > {q(log_path)} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {q(exit_path)} ) & "
            f"echo $! > {q(pid_path)} && cat {q(pid_path)}")
        result = env.execute(bg_command, cwd=cwd or "", timeout=timeout, rewrite_compound_background=False)
        output = result.get("output", "").strip()
        session.pid = next((int(ln) for ln in map(str.strip, output.splitlines()) if ln.isdigit()), None)
        # A wrapper without a PID never established a manageable background process.
        if session.pid is None:
            raise RuntimeError(f"launch wrapper exited {result.get('returncode')} without a PID: "
                               f"{output or '(no output)'}")
        self._track_started(
            session, self._env_poller_loop, f"proc-poller-{session.id}", (env, log_path, pid_path, exit_path))
        return session

    def _owned_reader_loop(self: Any, session: ProcessSession):
        from tools import process_registry as facade

        logger = facade.logger
        handle = session.owned_handle
        try:
            raw = handle.stdout.buffer
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            while True:
                chunk = raw.read1(4096)
                if not chunk:
                    break
                self._ingest_output(session, decoder.decode(chunk))
            tail = decoder.decode(b'', final=True)
            if tail:
                self._ingest_output(session, tail)
            receipt = session.env_ref.wait_owned(handle)
            session.owned_lifecycle = receipt
            cancelled = receipt.get('cancelled') and hasattr(session, '_owned_kill_source')
            self._finish_exited(session, handle.returncode,
                'killed' if cancelled else 'exited',
                getattr(session, '_owned_kill_source', '') if cancelled else '')
        except Exception as exc:
            # Unknown is still running/manageable. Do not publish a false exit.
            logger.warning('Owned backend completion unverified for %s: %s', session.id, exc, exc_info=True)
        finally:
            handle.stdout.close()

    def _kill_owned_session(self: Any, session, source, consume_output):
        from tools import process_registry as facade

        logger = facade.logger
        if session._completion_event.is_set():
            return self._owned_kill_snapshot(session, 'already_exited', consume_output)
        # The reader ingests the terminal tail before publishing completion; the
        # trusted scope report chooses the winner of the exit/cancel race.
        with session._lock:
            if not hasattr(session, '_owned_kill_source'):
                session._owned_kill_source = source
        try:
            receipt = session.env_ref.signal_owned(session.owned_handle)
        except Exception as exc:
            logger.warning('Owned kill unverified for %s', session.id, exc_info=True)
            return {'status': 'error', 'error': f'Owned kill unverified: {exc}',
                    'session_id': session.id, 'process_running': True}
        with session._lock:
            session.owned_lifecycle = receipt
        reader = session._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(2)
        status = 'killed' if receipt.get('cancelled') else 'already_exited'
        return self._owned_kill_snapshot(session, status, consume_output)

    def _owned_kill_snapshot(self: Any, session, status, consume_output):
        from tools import process_registry as facade

        # Termination is independently verified by signal_owned. Only the
        # reader can commit the final output, durable result and notification.
        with session._lock:
            pending = not session._completion_event.is_set()
            result = {'status': status, 'session_id': session.id,
                      **({} if pending else self._exit_fields(session)),
                      'owned_lifecycle': session.owned_lifecycle,
                      'process_running': False, 'output_pending': pending,
                      **facade._completion_output(session)}
        # Persistence takes _result_persist_lock before session._lock. Never
        # reverse that order from a kill snapshot racing the reader's save.
        if consume_output and not pending:
            self._record_completion_consumed(session)
            result.update(self._result_fields(session))
        return result

    @staticmethod
    def _owned_output_pending(session) -> dict:
        from tools import process_registry as facade

        logger = facade.logger
        receipt = session.owned_lifecycle
        handle = session.owned_handle
        if (handle is not None and receipt is None and not session._completion_event.is_set()
                and handle.poll() is not None):
            try:
                # Lifecycle transport can finish before its independent output
                # consumer. A read-only zero-wait receipt must not require kill.
                receipt = session.env_ref.wait_owned(handle, timeout=0)
                session.owned_lifecycle = receipt
            except Exception:
                logger.warning('Owned natural termination remains unverified for %s', session.id, exc_info=True)
        if (session.owned_handle is not None and not session._completion_event.is_set()
                and receipt and receipt.get('root_exit') is not None
                and receipt.get('descendant_empty') is True):
            return {'output_pending': True, 'process_running': False,
                    'owned_lifecycle': receipt}
        return {}
