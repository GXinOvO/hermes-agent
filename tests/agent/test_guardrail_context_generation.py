"""Regression: committed context loss re-arms reads, not failure budgets."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from agent import conversation_compression as compression
from agent.read_context import reset_read_context
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController


def _seed(controller):
    for index in range(5):
        controller.after_call('skill_view', {'name': 'demo'}, 'x' * 700, failed=False)
        if index < 4:
            controller.observe_call('skill_view', {'name': 'demo'}, 'x' * 700)
    controller.after_call('web_extract', {'url': 'bad'}, '{"error":"unavailable"}', failed=True)
    for _ in range(2):
        controller.before_call('web_search', {'query': 'counted'})


def test_committed_compaction_rearms_skill_reads_without_resetting_failure_or_caps():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    _seed(controller)
    compressor = SimpleNamespace(compression_count=1, record_completed_compaction=lambda **kwargs: None)
    agent = SimpleNamespace(session_id='owned', _memory_manager=None, event_callback=None,
                            context_compressor=compressor, tools=[], _tool_guardrails=controller)
    compression._finish_compaction_boundary(
        agent, [{'role': 'user', 'content': '[CONTEXT COMPACTION] summary'}],
        new_system_prompt='sys', old_session_id=None, in_place=True,
        compacted_in_place=True, session_commit_succeeded=True,
        defer_context_engine_notification=False, compression_made_progress=True,
        compression_used_fallback=False, compression_feasibility_skip=False, task_id='owned-task', history_rewritten=True)
    decision = controller.before_call('skill_view', {'name': 'demo'})
    assert decision.allows_execution, f'new context cannot reload pruned skill: {decision.code}'
    observation = controller.observe_call('skill_view', {'name': 'demo'}, 'x' * 700)
    assert observation.stub is None
    for _ in range(4):
        controller.after_call('web_extract', {'url': 'bad'}, '{"error":"unavailable"}', failed=True)
    assert controller.before_call('web_extract', {'url': 'bad'}).code == 'repeated_exact_failure_block'
    assert controller._turn_web_search_count == 2


def test_read_epoch_preserves_failure_counts_without_mutation():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    controller.after_call('web_extract', {'url': 'bad'}, '{"error":"unavailable"}', failed=True)
    reset_read_context(SimpleNamespace(_tool_guardrails=controller), 'task-with-no-content')
    for _ in range(4):
        controller.after_call('web_extract', {'url': 'bad'}, '{"error":"unavailable"}', failed=True)
    assert controller.before_call('web_extract', {'url': 'bad'}).code == 'repeated_exact_failure_block'


@pytest.mark.parametrize('outcome', ['committed', 'noop', 'raises'])
def test_public_compression_rearms_reads_only_after_context_rewrite(outcome):
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent
    agent = _make_agent('skill_view', platform='cron')
    _seed(agent._tool_guardrails)
    engine = MagicMock()
    engine.compression_count = 1
    engine.last_prompt_tokens = 0
    engine.last_completion_tokens = 0
    engine._last_summary_error = None
    engine._last_compress_aborted = False
    engine.get_active_compression_failure_cooldown.return_value = None
    engine.should_compress_info.return_value = (True, None)
    agent.context_compressor = engine
    agent.compression_in_place = True
    messages = [{'role': 'user', 'content': 'request' * 200}]
    if outcome == 'committed':
        engine.compress.return_value = [{'role': 'user', 'content': 'summary'}]
    elif outcome == 'noop':
        engine.compress.side_effect = lambda messages, **kwargs: messages
    else:
        engine.compress.side_effect = RuntimeError('synthetic compression failure')
    with patch.object(agent, '_persist_session'):
        if outcome == 'raises':
            with pytest.raises(RuntimeError, match='synthetic compression failure'):
                agent._compress_context(messages, 'sys', approx_tokens=10_000, force=True)
        else:
            agent._compress_context(messages, 'sys', approx_tokens=10_000, force=True)
    decision = agent._tool_guardrails.before_call('skill_view', {'name': 'demo'})
    assert decision.allows_execution is (outcome == 'committed')


@pytest.mark.parametrize('outcome', ['committed', 'noop', 'raises'])
def test_post_tool_prune_rearms_reads_only_on_a_committed_new_context(outcome):
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent
    from agent.turn_preflight import compress_after_tool_results
    agent = _make_agent('skill_view', platform='cron')
    _seed(agent._tool_guardrails)
    engine = MagicMock()
    engine.should_compress.return_value = False
    engine.should_compress_info.return_value = (False, None)
    engine.should_defer_preflight_to_real_usage.return_value = True
    engine.awaiting_real_usage_after_compression = False
    engine.last_prompt_tokens = 1
    agent.context_compressor = engine
    agent.compression_enabled = True
    messages = [{'role': 'user', 'content': 'continue'},
                {'role': 'tool', 'content': 'old skill body'}]
    if outcome == 'committed':
        engine.prune_tool_results_only.return_value = ([messages[0], {'role': 'tool', 'content': '[SKILL_PRUNED]'}], 1)
    elif outcome == 'noop':
        engine.prune_tool_results_only.return_value = (messages, 0)
    else:
        engine.prune_tool_results_only.side_effect = RuntimeError('synthetic prune failure')
    compress_after_tool_results(
        agent, messages=messages, system_message='sys', user_message='continue',
        active_system_prompt='sys', conversation_history=None, compression_attempts=0,
        max_compression_attempts=3, effective_task_id='guard-prune-task',
        final_response=None, turn_exit_reason='', current_turn_user_idx=0)
    decision = agent._tool_guardrails.before_call('skill_view', {'name': 'demo'})
    assert decision.allows_execution is (outcome == 'committed')
