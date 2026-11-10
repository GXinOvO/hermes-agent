import threading

import pytest

from gateway.session_context import scoped_current_session_id
from tools import terminal_tool as terminal_module
from tools.terminal_tool_lifecycle import _cleanup_inactive_envs, _teardown_env
from tests.tools import test_owned_process_native as native_fixtures

native = native_fixtures.native
terminal = native_fixtures.terminal
process = native_fixtures.process


@pytest.mark.parametrize('command,expected', [('exit 17', 17), ('sleep 60', -9)])
def test_environment_teardown_keeps_owned_reader_result(native, monkeypatch, command, expected):
    _, registry, _ = native
    entered, release = threading.Event(), threading.Event()
    original = registry._ingest_output

    def hold_output(session, text, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(session, text, **kwargs)

    monkeypatch.setattr(registry, '_ingest_output', hold_output)
    session = None
    try:
        with scoped_current_session_id('native-owner'):
            started = terminal('printf OWNED_TEARDOWN_TAIL; ' + command, background=True)
            session = registry.get(started['session_id'])
            session.notify_on_complete = True
            assert entered.wait(5)
            if expected == 17:
                assert session.owned_handle.done.wait(5)
            terminal_module._last_activity[session.task_id] = 0
            _cleanup_inactive_envs(lifetime_seconds=1)
            assert terminal_module._active_environments[session.task_id] is session.env_ref
            assert not session.env_ref.closed
            _teardown_env(session.env_ref, session.task_id)
            assert session.owned_handle.done.is_set()
            assert not session.exited
            assert not session._completion_event.is_set()
            assert registry.completion_queue.empty()
            release.set()
            assert session._completion_event.wait(5)
            result = process('poll', session.id)
        assert result['exit_code'] == expected
        assert result['completion_reason'] == 'exited'
        assert result['result_durability'] == 'persisted'
        assert result['output_preview'] == 'OWNED_TEARDOWN_TAIL'
        notice = registry.completion_queue.get_nowait()
        assert notice['exit_code'] == expected
        assert notice['output'] == 'OWNED_TEARDOWN_TAIL'
        assert registry.completion_queue.empty()
    finally:
        release.set()
        if session is not None and session._reader_thread is not None:
            session._reader_thread.join(5)


@pytest.mark.parametrize('cache_key', ['default', 'session:native-owner'])
def test_lost_owned_backend_is_reaped_and_next_command_rebuilds(native, monkeypatch, cache_key):
    _, registry, _ = native
    monkeypatch.setattr(terminal_module, '_resolve_container_task_id', lambda _task_id: cache_key)
    monkeypatch.setattr(terminal_module, '_calls_in_flight', {})
    monkeypatch.setattr(terminal_module, '_creation_locks', {})
    with scoped_current_session_id('native-owner'):
        started = terminal('printf OWNED_STARTED; sleep 60', background=True)
        session = registry.get(started['session_id'])
        session.notify_on_complete = True
        env = session.env_ref
        env.proc.kill()
        env.proc.wait(5)
        env.reader.join(5)
        session._reader_thread.join(5)
        assert not session._reader_thread.is_alive()
        assert session.owned_handle.done.is_set()
        assert session.owned_handle.lifecycle is None
        terminal_module._last_activity[cache_key] = 0
        _cleanup_inactive_envs(lifetime_seconds=1)
        assert cache_key not in terminal_module._active_environments
        assert env.closed and env.cleanup_complete
        assert not session.exited and not session._completion_event.is_set()
        assert registry.completion_queue.empty()
        recovered = terminal('printf OWNED_REBUILT')
        assert recovered['exit_code'] == 0
        assert recovered['output'] == 'OWNED_REBUILT'
        assert terminal_module._active_environments[cache_key] is not env
