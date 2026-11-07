import json
import sqlite3

import pytest

from tools import file_state
from tools.delegate_tool import _run_single_child
from tools.process_registry_notifications import format_process_notification
from tests.tools import test_delegate_output_schema as fixtures


@pytest.mark.parametrize('schema,raw,valid', [
    ({'type': 'object'}, '{"city":"Berlin"}', True),
    ({}, '["city",7]', True),
    ({'type': 'object'}, 'invalid output', False),
])
@pytest.mark.parametrize('advisory', ['write', 'steer', 'both'])
def test_structured_summary_survives_runtime_advisories(tmp_path, monkeypatch, schema, raw, valid, advisory):
    monkeypatch.setattr(file_state, '_registry', file_state.FileStateRegistry())
    path = tmp_path / 'shared.txt'
    path.write_text('original')

    class Parent(fixtures._StubParent):
        _current_task_id = 'structured-advisory-parent'

    class Child(fixtures._StubChild):
        def run_conversation(self, user_message, task_id=None, **kwargs):
            if advisory in ('write', 'both'):
                path.write_text('concurrent edit')
                file_state.note_write('independent-sibling', path)
            result = super().run_conversation(user_message, task_id=task_id, **kwargs)
            if advisory in ('steer', 'both'):
                result['pending_steer'] = 'review the next item'
            return result

    file_state.record_read(Parent._current_task_id, path)
    child = Child([raw, raw])
    child._delegate_output_schema = schema
    entry = _run_single_child(0, 'return structured output', child, Parent())
    assert entry['summary'] == raw
    assert entry['schema_valid'] is valid
    assert entry['status'] == 'completed'
    if valid:
        assert json.loads(entry['summary']) == json.loads(raw)
    if advisory in ('write', 'both'):
        assert entry['stale_paths'] == [str(path)]
    if advisory in ('steer', 'both'):
        assert entry['missed_steer'] == 'review the next item'
    from tools import async_delegation as ledger
    from tools.delegate_tool_results import _finalize_child_results

    parent = Parent()
    _finalize_child_results([entry], [{'goal': 'return structured output'}],
                            [(0, {'goal': 'return structured output'}, child)], parent)
    ledger._persist_dispatch({'delegation_id': 'structured-advisory', 'dispatched_at': 1})
    event = {'type': 'async_delegation', 'delegation_id': 'structured-advisory',
             'dispatched_at': 1, 'completed_at': 2, 'status': 'completed',
             'results': [entry], 'goals': ['return structured output']}
    ledger._persist_completion(event, {'results': [entry]})
    with sqlite3.connect(ledger._db_path()) as conn:
        event_json, result_json = conn.execute(
            'SELECT event_json,result_json FROM async_delegations WHERE delegation_id=?',
            ('structured-advisory',)).fetchone()
    stored = json.loads(result_json)['results'][0]
    assert stored['summary'] == raw
    assert stored['schema_valid'] is valid
    assert json.loads(event_json)['results'][0] == stored
    for batch in (True, False):
        event = {'type': 'async_delegation', 'delegation_id': 'structured-advisory',
                 'dispatched_at': 1, 'completed_at': 2}
        if batch:
            event.update(results=[stored], goals=['return structured output'])
        else:
            event.update(stored)
        notice = format_process_notification(event)
        assert raw in notice
        if advisory in ('write', 'both'):
            assert str(path) in notice
            assert 're-read before editing' in notice
        if advisory in ('steer', 'both'):
            assert 'review the next item' in notice
            assert 'steer did not land' in notice


def test_unstructured_summary_keeps_legacy_advisories(tmp_path, monkeypatch):
    monkeypatch.setattr(file_state, '_registry', file_state.FileStateRegistry())
    path = tmp_path / 'shared.txt'
    path.write_text('original')

    class Parent(fixtures._StubParent):
        _current_task_id = 'legacy-advisory-parent'

    class Child(fixtures._StubChild):
        def run_conversation(self, user_message, task_id=None, **kwargs):
            path.write_text('concurrent edit')
            file_state.note_write('independent-sibling', path)
            result = super().run_conversation(user_message, task_id=task_id, **kwargs)
            result['pending_steer'] = 'review the next item'
            return result

    file_state.record_read(Parent._current_task_id, path)
    entry = _run_single_child(0, 'return text', Child(['finished']), Parent())
    assert entry['summary'].startswith('finished')
    assert 'schema_valid' not in entry
    assert str(path) in entry['summary']
    assert 're-read before editing' in entry['summary']
    assert 'steer did not land' in entry['summary']
    assert entry['missed_steer'] == 'review the next item'
