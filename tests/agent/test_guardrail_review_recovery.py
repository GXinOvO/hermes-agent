"""Independent bounded controls; imports candidate code through native runner."""
import copy
import json
from unittest.mock import patch

import pytest

from agent import conversation_compression as compression
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
from tests.agent.test_compaction_events import _Agent, _compressor, _messages
from hermes_state import SessionDB


def _seed_reads(controller):
    for _ in range(5):
        controller.after_call('skill_view', {'name': 'demo'}, 'same rules', failed=False)
    assert copy.deepcopy(controller).before_call('skill_view', {'name': 'demo'}).code == 'idempotent_no_progress_block'
    assert controller.halt_decision is None, 'seed counts without making a halt decision on the live controller'


@pytest.mark.parametrize('in_place', [True, False])
def test_failed_commit_preserves_read_guard_and_original_context(tmp_path, in_place):
    agent = _Agent(_compressor())
    agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    _seed_reads(agent._tool_guardrails)
    agent._session_db = SessionDB(db_path=tmp_path / 'state.db')
    agent._session_db.create_session(agent.session_id, 'cli', model=agent.model)
    agent.compression_in_place = in_place
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db = lambda *args, **kwargs: None
    messages = _messages()
    original = copy.deepcopy(messages)
    target = (patch.object(SessionDB, 'archive_and_compact', side_effect=OSError('review synthetic disk full'))
              if in_place else patch.object(compression, '_publish_rotated_compaction',
                                            side_effect=OSError('review synthetic publication failure')))
    with patch.object(agent.context_compressor, '_generate_summary', return_value='SANITIZED SUMMARY'), target:
        returned, _ = compression.compress_context(agent, messages, 'sys', approx_tokens=80_000, force=True)
    assert returned == original, 'the failed commit must have rolled back history'
    assert returned is messages
    verdict = agent._tool_guardrails.before_call('skill_view', {'name': 'demo'})
    print(json.dumps({'case': 'failed_commit', 'in_place': in_place,
                      'history_rolled_back': returned == original,
                      'read_verdict_after_failure': verdict.code}))
    assert verdict.code == 'idempotent_no_progress_block', 'failed rewrite rearmed reads without discarding payload'


def test_same_successful_command_with_alternating_results_cannot_rearm_reads_forever():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    skill = {'name': 'demo'}
    command = {'command': 'cat /var/run/review-status'}
    verdict = controller.before_call('skill_view', skill)
    for index in range(12):
        # Varied read-only calls break a short identical cycle, as in the candidate's own no-op control.
        calls = [('skill_view', skill, 'same rules'),
                 ('terminal', command, json.dumps({'exit_code': 0, 'output': ['idle', 'waiting'][index % 2]})),
                 ('read_file', {'path': f'/unchanged/read-only-{index}'}, 'same bytes')]
        for name, args, result in calls:
            verdict = controller.before_call(name, args)
            if not verdict.allows_execution:
                break
            controller.after_call(name, args, result, failed=False)
            controller.observe_call(name, args, result)
        if not verdict.allows_execution:
            break
    if verdict.allows_execution:
        verdict = controller.before_call('skill_view', skill)
    print(json.dumps({'case': 'replayed_command_alternating_output', 'phases': 12,
                      'read_verdict': verdict.code,
                      'halt': controller.halt_decision.code if controller.halt_decision else None}))
    assert verdict.code == 'idempotent_no_progress_block', 'same successful read-only command replay reset every read epoch'


def test_replayed_alternating_patch_results_are_not_new_progress():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    for index in range(12):
        controller.after_call('skill_view', {'name': 'demo'}, 'rules', failed=False)
        controller.after_call('patch', {'path': 'same.py'},
                              json.dumps({'success': True, 'diff': ['A', 'B'][index % 2]}), failed=False)
    assert controller.before_call('skill_view', {'name': 'demo'}).code == 'idempotent_no_progress_block'


@pytest.mark.parametrize('mode', ['no_db', 'no_db_in_place', 'postcommit_failure'])
def test_surviving_rewrite_rearms_read_guards(mode, tmp_path):
    agent = _Agent(_compressor())
    agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    _seed_reads(agent._tool_guardrails)
    agent.compression_in_place = True
    messages = _messages()
    if mode == 'postcommit_failure':
        agent._session_db = SessionDB(db_path=tmp_path / 'state.db')
        agent._session_db.create_session(agent.session_id, 'cli', model=agent.model)
        agent._persist_user_message_idx = 0
        target = patch.object(SessionDB, 'update_system_prompt', side_effect=OSError('synthetic postcommit failure'))
    else:
        from contextlib import nullcontext
        target = nullcontext()
    if mode == 'no_db_in_place':
        def rewrite(held, **kwargs):
            held[:] = [held[0], {'role': 'user', 'content': 'summary'}, held[-1]]
            return held
        engine = patch.object(agent.context_compressor, 'compress', side_effect=rewrite)
        fence = compression.CompressionCommitFence()
    else:
        engine = patch.object(agent.context_compressor, '_generate_summary', return_value='SANITIZED SUMMARY')
        fence = None
    with engine, target:
        returned, _ = compression.compress_context(agent, messages, 'sys', approx_tokens=80_000,
                                                   force=True, commit_fence=fence)
    assert len(returned) < len(_messages())
    assert agent._tool_guardrails.before_call('skill_view', {'name': 'demo'}).allows_execution
