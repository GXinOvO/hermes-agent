"""Read progression regression for long single-turn unattended work."""
import json
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController


def test_successful_progress_allows_later_skill_reload_without_unbounded_cycles():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    args = {'name': 'test-driven-development'}
    body = json.dumps({'success': True, 'content': 'TDD rules'})
    # Distinct successful output represents successive real work, not a repeated batch.
    for index in range(8):
        decision = controller.before_call('skill_view', args)
        assert decision.allows_execution, f'progressing task lost skill access at phase {index}: {decision.code}'
        controller.after_call('skill_view', args, body, failed=False)
        controller.observe_call('skill_view', args, body)
        result = json.dumps({'bytes_written': index + 1, 'path': f'leaf-{index}.py'})
        controller.after_call('write_file', {'path': f'leaf-{index}.py'}, result, failed=False)
        controller.observe_call('write_file', {'path': f'leaf-{index}.py'}, result)
    # A fixed read/write batch with identical arguments and results must still halt.
    for _ in range(8):
        controller.after_call('skill_view', args, body, failed=False)
        controller.observe_call('skill_view', args, body)
        controller.after_call('write_file', {'path': 'same.py'}, 'ok', failed=False)
        controller.observe_call('write_file', {'path': 'same.py'}, 'ok')
        if controller.halt_decision is not None:
            break
    assert controller.halt_decision is not None
    assert controller.halt_decision.code == 'identical_cycle_halt'


def test_identical_successful_command_cannot_rearm_reads_forever():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    args = {'name': 'test-driven-development'}
    for index in range(8):
        controller.after_call('skill_view', args, 'rules', failed=False)
        controller.after_call('terminal', {'command': 'true'}, '{"exit_code":0}', failed=False)
        # Varied read args break short cycle detection, but provide no new mutation.
        controller.after_call('read_file', {'path': f'/item-{index}'}, 'same', failed=False)
    assert controller.before_call('skill_view', args).code == 'idempotent_no_progress_block'


def test_failed_mutation_does_not_rearm_redundant_reads():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    args = {'name': 'test-driven-development'}
    for index in range(5):
        controller.after_call('skill_view', args, 'rules', failed=False)
        controller.after_call('write_file', {'path': f'leaf-{index}.py'}, '{"error":"denied"}', failed=True)
    assert controller.before_call('skill_view', args).code == 'idempotent_no_progress_block'
