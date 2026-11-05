"""A real Agent guard halt cannot become a successful scheduled result."""
import json
from unittest.mock import patch
import pytest
from cron.scheduler import _final_response_from_result
from tests.agent import test_tool_call_guardrail_runtime as fixtures


def test_agent_guard_halt_is_failed_and_cron_rejects_its_real_result():
    agent = fixtures._make_agent('skill_view', max_iterations=12, platform='cron')
    agent._disable_streaming = True
    args = {'name': 'demo-skill'}
    agent.client.chat.completions.create.side_effect = [
        fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
            fixtures._mock_tool_call('skill_view', json.dumps(args), f'call-{index}')])
        for index in range(10)
    ]
    with (patch('model_tools.handle_function_call', return_value=json.dumps({'success': True, 'content': 'same rules'})),
          patch.object(agent, '_persist_session'), patch.object(agent, '_save_trajectory'),
          patch.object(agent, '_cleanup_task_resources')):
        result = agent.run_conversation('read the same skill repeatedly')
    assert result['turn_exit_reason'] == 'guardrail_halt'
    assert result['guardrail']['action'] in {'block', 'halt'}
    assert result['failed'] is True, 'a controlled stop is not successful execution'
    assert result['completed'] is False
    assert result['final_response']
    with pytest.raises(RuntimeError, match='stopped retrying'):
        _final_response_from_result(result, 'guard-test', 'bounded guard test', type(agent))
