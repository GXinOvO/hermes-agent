import json
import threading
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import cleanup_task_resources
from tests.tools import test_delegate_output_schema as fixtures
from tools import file_state
from tools.delegate_tool import _run_single_child
from tools.file_tools import read_file_tool, write_file_tool
from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(file_state, '_registry', file_state.FileStateRegistry())
    return file_state.get_registry()


@pytest.mark.parametrize('schema,raw', [({'type': 'object'}, '{"status":"ok"}'), ({}, '["ok",1]'), ({}, 'null'), (None, 'finished')])
@pytest.mark.parametrize('nested', [False, True])
def test_completed_child_write_survives_real_turn_cleanup(tmp_path, registry, schema, raw, nested):
    path = tmp_path / 'seed.txt'
    path.write_text('before\n')

    class Parent(fixtures._StubParent):
        _current_task_id = 'observation-parent'

    class Child(fixtures._StubChild):
        def run_conversation(self, user_message, task_id=None, **kwargs):
            assert isinstance(task_id, str)
            writer = task_id + '-nested' if nested else task_id
            assert not json.loads(read_file_tool(str(path), task_id=writer)).get('error')
            assert json.loads(write_file_tool(str(path), 'after\n', task_id=writer))['verified']
            cleanup_task_resources(SimpleNamespace(verbose_logging=False), writer)
            assert file_state.writes_since(Parent._current_task_id, 0, [path]) == {}
            return super().run_conversation(user_message, task_id=task_id, **kwargs)

    assert not json.loads(read_file_tool(str(path), task_id=Parent._current_task_id)).get('error')
    child = Child([raw])
    child._delegate_output_schema = schema
    entry = _run_single_child(0, 'write then finish', child, Parent())
    assert entry['status'] == 'completed'
    assert path.read_bytes() == b'after\n'
    if schema is not None:
        assert entry['schema_valid'] is True
        assert entry['summary'] == raw
        assert entry['stale_paths'] == [str(path)]
        event = {'type': 'async_delegation', 'delegation_id': 'fixture-not-delivered',
                 'dispatched_at': 1, 'completed_at': 2, 'results': [entry], 'goals': ['write then finish']}
        notice = format_process_notification(event)
        assert raw in notice and 're-read before editing' in notice and str(path) in notice
    else:
        assert 're-read before editing' in entry['summary'] and str(path) in entry['summary']
    assert not registry._write_observers
    assert not any(writer.endswith('-nested') for writer, _ in registry._last_writer.values())


@pytest.mark.parametrize('failure', ['child', 'validation'])
def test_observer_released_when_native_child_path_fails(tmp_path, registry, monkeypatch, failure):
    from tools import delegate_tool

    path = tmp_path / 'seed.txt'
    path.write_text('before')

    class Parent(fixtures._StubParent):
        _current_task_id = 'failure-parent'

    class Child(fixtures._StubChild):
        def run_conversation(self, user_message, task_id=None, **kwargs):
            assert registry._write_observers
            if failure == 'child':
                raise RuntimeError('synthetic child exception')
            return super().run_conversation(user_message, task_id=task_id, **kwargs)

    if failure == 'validation':
        def reject(*args, **kwargs):
            raise RuntimeError('synthetic validator exception')
        monkeypatch.setattr(delegate_tool, '_validate_child_output_schema', reject)
    file_state.record_read(Parent._current_task_id, path)
    entry = _run_single_child(0, 'exercise failure cleanup', Child(['finished']), Parent())
    assert entry['status'] == 'error'
    assert not registry._write_observers


def test_observers_keep_distinct_parent_paths_and_close_independently(tmp_path, registry):
    a, b = (str(tmp_path / name) for name in ('a', 'b'))
    registry.record_read('parent-A', a, mtime=1)
    registry.record_read('parent-B', b, mtime=1)
    start, _, first = registry.observe_writes('parent-A')
    _, _, second = registry.observe_writes('parent-B')
    assert first is not None and second is not None
    barrier = threading.Barrier(3)
    errors = []

    def writer(task_id, path):
        try:
            barrier.wait(timeout=3)
            registry.note_write(task_id, path, mtime=1)
            registry.forget_task(task_id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=('child-A', a)), threading.Thread(target=writer, args=('child-B', b))]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
        assert not errors and not any(thread.is_alive() for thread in threads)
        assert registry.writes_since('', start, [a, b]) == {}
        assert first.snapshot() == [a]
        assert second.snapshot() == [b]
        first.close()
        first.close()
        registry.note_write('later-child', a, mtime=2)
        assert second.snapshot() == [b]
        assert len(registry._write_observers) == 1
    finally:
        first.close()
        second.close()
        for thread in threads:
            thread.join(timeout=3)
    assert not registry._write_observers


def test_observer_ignores_parent_old_and_unwatched_writes(tmp_path, registry):
    path, other = (str(tmp_path / name) for name in ('watched', 'other'))
    registry.note_write('old-child', path, mtime=1)
    registry.record_read('parent', path, mtime=1)
    _, _, observer = registry.observe_writes('parent')
    assert observer is not None
    try:
        registry.note_write('parent', path, mtime=2)
        registry.note_write('child', other, mtime=2)
        assert observer.snapshot() == []
        registry.note_write('child', path, mtime=3)
        registry.forget_task('child')
        assert observer.snapshot() == [path]
    finally:
        observer.close()


def test_closed_observer_no_longer_collects_writes(tmp_path, registry):
    path = str(tmp_path / 'seed')
    registry.record_read('parent', path, mtime=1)
    _, _, observer = registry.observe_writes('parent')
    assert observer is not None
    observer.close()
    registry.note_write('child', path, mtime=1)
    assert observer.snapshot() == []
    assert not registry._write_observers


def test_observer_respects_disabled_file_state_guard(tmp_path, registry, monkeypatch):
    path = str(tmp_path / 'seed')
    registry.record_read('parent', path, mtime=1)
    _, _, observer = registry.observe_writes('parent')
    assert observer is not None
    try:
        monkeypatch.setenv('HERMES_DISABLE_FILE_STATE_GUARD', '1')
        registry.note_write('child', path, mtime=1)
        assert observer.snapshot() == []
        monkeypatch.delenv('HERMES_DISABLE_FILE_STATE_GUARD')
        assert observer.snapshot() == []
        registry.note_write('child', path, mtime=2)
        assert observer.snapshot() == [path]
    finally:
        observer.close()
    assert not registry._write_observers


def test_delegation_observation_is_bounded_by_parent_read_population(tmp_path, registry):
    paths = [str(tmp_path / f'seed-{index}') for index in range(file_state._MAX_PATHS_PER_AGENT + 1)]
    for path in paths:
        registry.record_read('parent', path, mtime=1)
    watched = registry.known_reads('parent')
    assert len(watched) == file_state._MAX_PATHS_PER_AGENT
    _, snapshot, observer = registry.observe_writes('parent')
    assert snapshot == watched and observer is not None
    try:
        for path in paths:
            registry.note_write('child', path, mtime=2)
        registry.forget_task('child')
        assert len(observer.paths) == file_state._MAX_PATHS_PER_AGENT
        assert observer.snapshot() == sorted(registry.display_paths('parent', watched))
        assert not registry._last_writer
    finally:
        observer.close()
    assert not registry._write_observers


def test_registration_captures_waiting_native_sibling_write(tmp_path, registry, monkeypatch):
    path = tmp_path / 'registration.txt'
    path.write_text('before\n')
    parent_id = 'registration-parent'
    writer_id = 'registration-sibling'
    assert not json.loads(read_file_tool(str(path), task_id=parent_id)).get('error')
    assert not json.loads(read_file_tool(str(path), task_id=writer_id)).get('error')
    start = threading.Event()
    at_write_lock = threading.Event()
    done = threading.Event()
    errors = []
    original_lock = registry._state_lock
    original_observer = file_state._WriteObserver

    class StateLock:
        def __enter__(self):
            if threading.current_thread() is sibling:
                at_write_lock.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    def write_and_cleanup():
        try:
            assert start.wait(timeout=5)
            assert json.loads(write_file_tool(str(path), 'sibling-after\n', task_id=writer_id))['verified']
            cleanup_task_resources(SimpleNamespace(verbose_logging=False), writer_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    sibling = threading.Thread(target=write_and_cleanup)
    monkeypatch.setattr(registry, '_state_lock', StateLock())

    def constructing(*args, **kwargs):
        start.set()
        assert at_write_lock.wait(timeout=5)
        if not original_lock.locked():
            assert done.wait(timeout=5)
        return original_observer(*args, **kwargs)

    monkeypatch.setattr(file_state, '_WriteObserver', constructing)

    class Parent(fixtures._StubParent):
        _current_task_id = parent_id

    class Child(fixtures._StubChild):
        def run_conversation(self, *args, **kwargs):
            assert done.wait(timeout=5)
            return super().run_conversation(*args, **kwargs)

    sibling.start()
    child = Child(['{"status":"ok"}'])
    child._delegate_output_schema = {'type': 'object'}
    try:
        entry = _run_single_child(0, 'exercise registration boundary', child, Parent())
    finally:
        start.set()
        sibling.join(timeout=5)
    assert not sibling.is_alive() and not errors
    assert path.read_bytes() == b'sibling-after\n'
    assert entry['status'] == 'completed' and entry['schema_valid'] is True
    assert entry['summary'] == '{"status":"ok"}'
    assert registry.writes_since(parent_id, 0, [str(path)]) == {}
    assert not registry._write_observers
    assert entry.get('stale_paths') == [str(path)], 'SIBLING_WRITE_LOST_DURING_SEED_REGISTRATION'


@pytest.mark.parametrize('disabled', [False, True])
def test_unwatched_or_disabled_parent_allocates_no_observer(tmp_path, registry, monkeypatch, disabled):
    if disabled:
        registry.record_read('parent', str(tmp_path / 'seed'), mtime=1)
        monkeypatch.setenv('HERMES_DISABLE_FILE_STATE_GUARD', '1')
    _, paths, observer = registry.observe_writes('parent')
    assert paths == [] and observer is None and not registry._write_observers


def test_observation_linearization_does_not_depend_on_wall_clock(tmp_path, registry, monkeypatch):
    path = str(tmp_path / 'watched')
    later = str(tmp_path / 'read-after-registration')
    registry.record_read('parent', path, mtime=1)
    registry.note_write('old-sibling', path, mtime=1)
    registry.forget_task('old-sibling')
    monkeypatch.setattr(file_state.time, 'time', lambda: 10)
    start, paths, observer = registry.observe_writes('parent')
    assert start == 10 and registry.display_paths('parent', paths) == [path] and observer is not None
    try:
        assert observer.snapshot() == []
        registry.record_read('parent', later, mtime=1)
        registry.note_write('new-sibling', later, mtime=2)
        monkeypatch.setattr(file_state.time, 'time', lambda: 1)
        registry.note_write('new-sibling', path, mtime=2)
        registry.forget_task('new-sibling')
        assert registry.writes_since('parent', start, paths) == {}
        assert observer.snapshot() == [path]
    finally:
        observer.close()
    assert not registry._write_observers


def test_timeout_releases_observer_before_deferred_child_close(tmp_path, registry, monkeypatch):
    from tests.tools.test_delegate_timeout_cleanup import _SlowUnwindingChild
    from tools import delegate_tool
    from tools.daemon_pool import DaemonThreadPoolExecutor

    child = _SlowUnwindingChild()
    registry.record_read('timeout-parent', str(tmp_path / 'watched'), mtime=1)
    parent = SimpleNamespace(session_id='observation-timeout', _current_task_id='timeout-parent',
                             _active_children=[child], _active_children_lock=threading.Lock())
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda: 0.5)
    monkeypatch.setattr(delegate_tool, '_get_worktree_isolation', lambda: False)
    submit = DaemonThreadPoolExecutor.submit

    def started(executor, *args, **kwargs):
        future = submit(executor, *args, **kwargs)
        assert child.started.wait(timeout=5) and registry._write_observers
        return future

    monkeypatch.setattr(DaemonThreadPoolExecutor, 'submit', started)
    try:
        result = _run_single_child(0, 'timeout observation', child, parent)
        assert result['status'] == 'timeout'
        assert not registry._write_observers and not child.closed.is_set()
    finally:
        child.allow_finish.set()
        child.interrupted.set()
    assert child.finished.wait(timeout=5) and child.closed.wait(timeout=5)
    assert not child.close_while_running


def test_setup_cancellation_releases_observer(tmp_path, registry, monkeypatch):
    from tools import delegate_tool

    registry.record_read('cancel-parent', str(tmp_path / 'watched'), mtime=1)
    seed = delegate_tool._ChildRun.seed_workspace

    def cancel(run):
        seed(run)
        assert registry._write_observers
        raise KeyboardInterrupt('observation-cancellation-fixture')

    monkeypatch.setattr(delegate_tool._ChildRun, 'seed_workspace', cancel)
    with pytest.raises(KeyboardInterrupt, match='observation-cancellation-fixture'):
        _run_single_child(0, 'cancel observation', fixtures._StubChild(['unused']),
                          SimpleNamespace(_current_task_id='cancel-parent'))
    assert not registry._write_observers


def test_closed_observation_releases_instance_and_history(tmp_path, registry):
    import gc
    import weakref

    path = str(tmp_path / 'watched')
    registry.record_read('parent', path, mtime=1)
    _, _, observer = registry.observe_writes('parent')
    assert observer is not None
    registry.note_write('sibling', path, mtime=1)
    registry.forget_task('sibling')
    assert observer.snapshot() == [path] and not registry._last_writer
    ref = weakref.ref(observer)
    observer.close()
    del observer
    gc.collect()
    assert ref() is None
    _, _, later = registry.observe_writes('parent')
    assert later is not None
    try:
        assert later.snapshot() == []
    finally:
        later.close()
