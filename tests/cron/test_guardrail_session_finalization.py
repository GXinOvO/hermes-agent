"""Trace a real halted Agent's persisted message into Cron session finalization."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron.scheduler import _final_response_from_result, _finalize_cron_session
from hermes_state import SessionDB
from tests.agent import test_tool_call_guardrail_runtime as fixtures


def test_real_guard_halt_cannot_book_cron_session_complete(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid = 'review-real-guard-halt'
    agent_class = fixtures.AIAgent
    def with_db(*args, **kwargs):
        return agent_class(*args, **kwargs, session_db=db, session_id=sid)
    with patch.object(fixtures, 'AIAgent', side_effect=with_db):
        agent = fixtures._make_agent('skill_view', max_iterations=12, platform='cron')
    agent._disable_streaming = True
    args = {'name': 'demo-skill'}
    agent.client.chat.completions.create.side_effect = [
        fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
            fixtures._mock_tool_call('skill_view', json.dumps(args), f'call-{index}')])
        for index in range(10)
    ]
    with (patch('model_tools.handle_function_call', return_value=json.dumps({'success': True, 'content': 'same rules'})),
          patch.object(agent, '_save_trajectory'), patch.object(agent, '_cleanup_task_resources')):
        result = agent.run_conversation('read the same skill repeatedly')
    assert result['turn_exit_reason'] == 'guardrail_halt'
    assert result['failed'] is True and result['completed'] is False
    assert result['guardrail']['action'] in {'block', 'halt'}
    with pytest.raises(RuntimeError, match='stopped retrying'):
        _final_response_from_result(result, 'review-guard', 'review guard halt', type(agent))
    lifecycle = db.session_lifecycle_statuses([sid])[sid]
    with patch('hermes_state_registry.release_or_close'):
        _finalize_cron_session(db, agent, 'review-guard', 'review guard halt', sid)
    session = db.get_session(sid)
    print(json.dumps({'failed': result['failed'], 'completed': result['completed'],
                      'turn_exit_reason': result['turn_exit_reason'],
                      'persisted_tail_lifecycle': lifecycle, 'session_end_reason': session['end_reason']}))
    assert session['end_reason'] != 'cron_complete', 'a failed guard halt persisted as a healthy Cron session'
    assert session['end_reason'] == 'cron_guardrail_halt'
    db.close()


@pytest.mark.parametrize('role,text,expected', [
    ('assistant', 'normal answer', 'cron_complete'),
    ('assistant', 'warning: check the remaining item', 'cron_complete'),
    ('assistant', '[SILENT]', 'cron_complete'),
    ('user', 'interrupted request', 'cron_incomplete_no_output'),
])
def test_session_finalization_preserves_non_guard_outcomes(tmp_path, role, text, expected):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid = 'non-guard-finalization'
    db.create_session(sid, source='cron')
    db.append_message(sid, role, text)
    agent = SimpleNamespace(session_id=sid, _tool_guardrail_halt_decision=None)
    with patch('hermes_state_registry.release_or_close'):
        _finalize_cron_session(db, agent, 'normal-job', 'normal job', sid)
    assert db.get_session(sid)['end_reason'] == expected
    db.close()
