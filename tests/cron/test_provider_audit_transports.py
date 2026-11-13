from types import SimpleNamespace

import pytest

from cron import scheduler
from cron.provider_audit import FireAudit


@pytest.mark.parametrize('failed', [False, True])
def test_app_server_audit_records_actual_turn_input_and_unknown_response_identity(monkeypatch, tmp_path, failed):
    from agent import codex_runtime, conversation_loop

    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    audit = FireAudit({}, 'app-server', 'configured-model')
    calls = []

    def run_turn(**kwargs):
        calls.append(kwargs)
        if failed:
            raise RuntimeError('synthetic failure')
        return SimpleNamespace(interrupted=False, error=None, should_retire=False,
                               final_text='ok', thread_id='thread', turn_id='turn')

    agent = SimpleNamespace(
        platform='cron', event_callback=audit.on_agent_event,
        model='configured-model', provider='openai-codex', requested_provider='openai-codex',
        api_mode='codex_app_server', reasoning_config={'enabled': True, 'effort': 'max'},
        _codex_session=SimpleNamespace(run_turn=run_turn),
    )
    monkeypatch.setattr('tools.delegate_tool_scope.validate_restricted_route', lambda *a, **k: None)
    monkeypatch.setattr(codex_runtime, '_ensure_codex_session', lambda *a: None)
    monkeypatch.setattr(codex_runtime, '_start_codex_thread', lambda *a: None)
    monkeypatch.setattr(codex_runtime, '_codex_wire_model', lambda *a: 'gpt-4o')
    monkeypatch.setattr(codex_runtime, '_codex_turn_effort', lambda *a: 'high')
    monkeypatch.setattr(codex_runtime, '_codex_turn_service_tier', lambda *a: None)
    monkeypatch.setattr(codex_runtime, '_close_codex_session', lambda *a: None)
    monkeypatch.setattr(codex_runtime, '_consume_user_interrupt', lambda *a: (False, None))
    monkeypatch.setattr(codex_runtime, '_transform_codex_reply', lambda *a, **k: {'final_response': 'ok'})
    monkeypatch.setattr(codex_runtime, '_persist_projected_messages', lambda *a: False)
    monkeypatch.setattr(codex_runtime, '_finish_codex_turn', lambda *a, **k: {})
    monkeypatch.setattr('agent.turn_recovery.activate_codex_app_server_fallback', lambda *a: False)
    agent._run_codex_app_server_turn = lambda **kwargs: codex_runtime.run_codex_app_server_turn(agent, **kwargs)
    state = SimpleNamespace(user_message='private-body', original_user_message='private-body', messages=[],
                            effective_task_id='task', _should_review_memory=False, turn_id='first-turn')
    result = conversation_loop._codex_app_server_turn(agent, state)
    assert result['completed'] is not failed
    assert len(calls) == 1
    assert len(audit._request_attempts) == 1
    attempt = audit._request_attempts[0]
    assert attempt['wire_model'] == calls[0]['model']
    assert attempt['wire_reasoning'] == {'reasoning_effort': calls[0]['reasoning_effort']}
    assert attempt['response_model'] is None
    assert attempt['upstream_provider'] is None
    assert attempt['api_call_count'] == 1
    first_id = attempt['api_request_id']
    state.turn_id = 'second-turn'
    conversation_loop._codex_app_server_turn(agent, state)
    assert len(audit._request_attempts) == 2
    assert audit._request_attempts[1]['api_request_id'] != first_id


def test_anthropic_main_stream_records_actual_sdk_attempt(monkeypatch, tmp_path):
    from tests.agent.test_anthropic_stream_fallbacks import _anthropic_agent, _Ctx, _minimax_sdk_stream

    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    audit = FireAudit({}, 'anthropic-stream', 'MiniMax-M2')
    agent = _anthropic_agent(lambda: _Ctx(_minimax_sdk_stream()))
    agent.platform, agent.event_callback = 'cron', audit.on_agent_event
    agent._current_api_request_id, agent._current_api_call_count, agent._current_retry_count = 'anthropic-1', 1, 0
    audit.record_request_event('request', {'api_request_id': 'anthropic-1', 'api_call_count': 1, 'retry_count': 0})
    try:
        response = agent._interruptible_streaming_api_call({'model': 'MiniMax-M2', 'messages': [], 'max_tokens': 8})
    finally:
        agent.close()
    assert response.content[0].text == 'hi'
    assert len(audit._request_attempts) == 1
    assert audit._request_attempts[0]['sdk_attempts'][0]['wire_model'] == 'MiniMax-M2'


@pytest.mark.parametrize('field', [
    'api_request_id', 'api_call_count', 'retry_count', 'wire_observation', 'wire_model_observation',
    'response_model_observation', 'upstream_provider_observation', 'reasoning_effort', 'fallback_active',
    'model', 'provider', 'api_mode',
])
def test_audit_writer_does_not_persist_sensitive_identifiers(monkeypatch, tmp_path, field):
    import json

    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    audit = FireAudit({}, 'privacy-boundary', 'gpt-4o')
    payload = {'api_request_id': 'request-safe', 'api_call_count': 1, 'retry_count': 0,
               field: 'token=writer-private-value'}
    audit.record_request_event('sdk_request', payload)
    if field != 'api_request_id':
        assert audit._request_attempts
    audit.write({}, None)
    row = json.loads((tmp_path / 'cron' / 'usage_audit.jsonl').read_text())
    assert 'writer-private-value' not in json.dumps(row)


@pytest.mark.parametrize('stream', [False, True])
def test_bedrock_audit_records_each_physical_recovery_call(monkeypatch, stream):
    from agent import bedrock_adapter, chat_completion_helpers

    audit = FireAudit({}, 'bedrock-recovery', 'bedrock-model')
    agent = SimpleNamespace(platform='cron', event_callback=audit.on_agent_event, model='bedrock-model',
                            _current_api_request_id='bedrock-1', _current_api_call_count=1, _current_retry_count=0)
    audit.record_request_event('request', {'api_request_id': 'bedrock-1', 'api_call_count': 1, 'retry_count': 0})
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError('cachePoint unsupported')
        return {'stream': [], 'ok': True}

    monkeypatch.setattr(bedrock_adapter, '_get_bedrock_runtime_client', lambda *a: SimpleNamespace(converse=call, converse_stream=call))
    monkeypatch.setattr(bedrock_adapter, 'recover_from_cache_point_rejection',
                        lambda exc, kwargs: {**kwargs, 'inferenceConfig': {'maxTokens': 8}})
    monkeypatch.setattr(bedrock_adapter, 'normalize_converse_response', lambda value: value)
    chat_completion_helpers._bedrock_converse_call({'modelId': 'bedrock-model'}, stream=stream, audit_agent=agent)
    assert len(calls) == 2
    assert len(audit._request_attempts) == 1
    assert len(audit._request_attempts[0]['sdk_attempts']) == 2
    assert all(x['wire_model'] == 'bedrock-model' for x in audit._request_attempts[0]['sdk_attempts'])


def test_codex_responses_audit_records_sdk_call_after_sanitization():
    from agent.codex_runtime import run_codex_stream
    from tests.agent.test_codex_stream_supersession import _agent, _completed, _message_added

    agent, _ = _agent(supersede_after_checks=100)
    audit = FireAudit({}, 'codex-responses', 'gpt-4o')
    agent.platform, agent.model, agent.event_callback = 'cron', 'gpt-4o', audit.on_agent_event
    agent._current_api_request_id, agent._current_api_call_count, agent._current_retry_count = 'codex-1', 1, 0
    audit.record_request_event('request', {'api_request_id': 'codex-1', 'api_call_count': 1, 'retry_count': 0})
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return iter([_message_added(), _completed()])

    run_codex_stream(agent, {'model': 'gpt-4o', 'reasoning': {'effort': 'high'}},
                     client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    assert len(calls) == 1
    sdk = audit._request_attempts[0]['sdk_attempts']
    assert len(sdk) == 1
    assert sdk[0]['wire_model'] == calls[0]['model']
    assert sdk[0]['wire_reasoning'] == {'reasoning': calls[0]['reasoning']}


def test_anthropic_nonstream_audit_records_final_filtered_kwargs():
    from agent.client_lifecycle import ClientLifecycleMixin

    audit = FireAudit({}, 'anthropic-nonstream', 'claude-test')
    agent = SimpleNamespace(platform='cron', event_callback=audit.on_agent_event, model='claude-test',
                            _disable_streaming=True, _capture_anthropic_response_headers=lambda *a: None,
                            _current_api_request_id='anthropic-2', _current_api_call_count=1, _current_retry_count=0)
    audit.record_request_event('request', {'api_request_id': 'anthropic-2', 'api_call_count': 1, 'retry_count': 0})
    calls = []
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or 'ok'))
    result = ClientLifecycleMixin._anthropic_messages_create(agent, {
        'model': 'claude-test', 'messages': [], 'max_tokens': 8, 'stream': False,
        'thinking': {'type': 'enabled', 'budget_tokens': 1024},
    }, client=client)
    assert result == 'ok'
    sdk = audit._request_attempts[0]['sdk_attempts']
    assert len(sdk) == 1
    assert sdk[0]['wire_model'] == calls[0]['model']
    assert sdk[0]['wire_reasoning']['thinking'] == calls[0]['thinking']


@pytest.mark.parametrize('field', ['api_request_id', 'model', 'provider', 'api_mode'])
@pytest.mark.parametrize('value', ['route?token=private-marker', 'route token=private-marker', 'route;secret=private-marker'])
def test_writer_rejects_prefixed_secret_assignments(monkeypatch, tmp_path, field, value):
    import json

    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    audit = FireAudit({}, 'prefixed-secret', 'gpt-4o')
    audit.record_request_event('request', {
        'api_request_id': 'request-safe', 'api_call_count': 1, 'retry_count': 0, field: value,
    })
    audit.write({}, None)
    record = json.loads((tmp_path / 'cron' / 'usage_audit.jsonl').read_text())
    assert 'private-marker' not in json.dumps(record)


@pytest.mark.parametrize('terminal_model', [None, 'gpt-4.1'])
def test_codex_response_identity_comes_from_terminal_event(terminal_model):
    from agent.codex_runtime import run_codex_stream
    from agent.provider_request_audit import response_snapshot
    from tests.agent.test_codex_stream_supersession import _agent, _completed, _message_added

    agent, _ = _agent(supersede_after_checks=100)
    agent.model = 'gpt-4o'
    terminal = _completed()
    if terminal_model:
        terminal.response.model = terminal_model
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter([_message_added(), terminal])))
    response = run_codex_stream(agent, {'model': agent.model}, client=client)
    snapshot = response_snapshot(agent, response, api_request_id='codex-terminal', api_call_count=1, retry_count=0)
    assert snapshot['response_model'] == terminal_model
    assert snapshot['response_model_observation'] == ('observed' if terminal_model else 'absent')


@pytest.mark.parametrize('model', ['amazon.nova-2-lite-v1:0', 'anthropic.claude-sonnet-4-6'])
def test_bedrock_reasoning_containers_are_recorded_from_real_builder(model, monkeypatch):
    from agent.bedrock_adapter import build_converse_kwargs
    from agent.provider_request_audit import wire_reasoning

    kwargs = build_converse_kwargs(model, [{'role': 'user', 'content': 'private-body'}],
                                   reasoning_config={'enabled': True, 'effort': 'high'}, prompt_cache=False)
    fields = kwargs['additionalModelRequestFields']
    reasoning = wire_reasoning(kwargs)
    expected = {key: fields[key] for key in ('thinking', 'reasoningConfig', 'output_config') if key in fields}
    assert reasoning['additionalModelRequestFields'] == expected
    from agent.provider_request_audit import observe_sdk_request
    audit = FireAudit({}, 'bedrock-persistence', model)
    agent = SimpleNamespace(platform='cron', event_callback=audit.on_agent_event, model=model,
                            _current_api_request_id='bedrock-reasoning', _current_api_call_count=1,
                            _current_retry_count=0)
    observe_sdk_request(agent, kwargs)
    records = []
    monkeypatch.setattr(scheduler, '_write_usage_audit', records.append)
    audit.write({}, None)
    assert records[0]['wire_reasoning']['additionalModelRequestFields'] == expected
    assert 'private-body' not in str(records)


def test_anthropic_adaptive_effort_is_recorded_without_other_output_config():
    from agent.provider_request_audit import wire_reasoning

    assert wire_reasoning({'thinking': {'type': 'adaptive'}, 'output_config': {
        'effort': 'high', 'format': {'description': 'private-format'},
    }}) == {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'high'}}


def test_private_sdk_model_is_labelled_withheld():
    from agent.provider_request_audit import observe_sdk_request

    events = []
    agent = SimpleNamespace(platform='cron', event_callback=lambda name, payload: events.append(payload),
                            model='configured-model', _current_api_request_id='private-model',
                            _current_api_call_count=1, _current_retry_count=0)
    observe_sdk_request(agent, {'model': 'private-relay-model-v7'})
    assert events[0]['wire_model'] is None
    assert events[0]['wire_model_observation'] == 'withheld'
