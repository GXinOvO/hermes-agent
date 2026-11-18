import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from cron import scheduler
from cron import memory_audit_policy
from gateway import session_cron
from persistence_assembly import open_persistence, profile_scope
from persistence.conversation.sessions import NewSession
from persistence.sqlite.conversation._store.messages import append_messages_batch as _append_batch
from persistence.conversation.messages import TranscriptRead, TranscriptView
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import transcript_rows
from tests.agent import test_tool_call_guardrail_runtime as fixtures
from tools.memory_tool_store import MemoryStore

CANDIDATE = Path(__file__).resolve().parents[2]
OLD = 'User prefers short explanations.'
NEW = 'User prefers short explanations with examples.'
OTHER = 'The unrelated fixture entry stays unchanged.'


def _audit_window_timestamp() -> float:
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    cutoff = now.replace(hour=3, minute=15, second=0, microsecond=0)
    if cutoff > now:
        cutoff -= timedelta(days=1)
    return (cutoff - timedelta(hours=1)).timestamp()


@pytest.mark.parametrize('scenario', [
    'complete_success', 'complete_both_success', 'complete_stale', 'complete_add_stale', 'complete_other_stale', 'complete_legacy_patch', 'complete_unpinned_patch',
    'partial_forced_write', 'partial_verified_merge', 'partial_new_add',
    'partial_mixed_batch', 'partial_duplicate_stale', 'partial_report_binding', 'partial_report_binding_symlink_home', 'partial_unknown_bot', 'partial_empty',
    'partial_report_missing', 'partial_report_valid', 'partial_report_bad_receipt',
    'partial_hidden_notice', 'partial_summary_tail', 'partial_api_sidecar', 'partial_absorbed_content',
    'invalid_publication', 'collection_error', 'collection_error_publisher_failure',
    'complete_prompt_missing', 'early_empty', 'early_wake_false', 'partial_external_before_publish',
    'complete_invisible_unicode',
])
# health: allow CC, FUNC_LINES, HX011 -- one integration matrix covers every audit outcome and its final bytes
def test_cron_scheduler_to_real_agent_and_memory_tool(tmp_path, monkeypatch, scenario):
    import run_agent
    import hermes_state_registry
    from tools import memory_tool

    assert Path(scheduler.__file__).resolve() == CANDIDATE / 'cron/scheduler.py'
    assert Path(memory_tool.__file__).resolve() == CANDIDATE / 'tools/memory_tool.py'
    assert (CANDIDATE / 'scripts' / memory_audit_policy.SCRIPT_NAME).is_file()
    assert not (CANDIDATE / 'collector.py').exists()
    home = tmp_path / 'home'
    scripts = home / 'scripts'
    memories = home / 'memories'
    report_dir = home / 'reports' / 'builtin-memory-audit-trial-20261110'
    for directory in (scripts, memories, report_dir):
        directory.mkdir(parents=True)
    configured_home = home
    if scenario == 'partial_report_binding_symlink_home':
        configured_home = tmp_path / 'linked-home'
        configured_home.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv('HERMES_HOME', str(configured_home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    user_path = memories / 'USER.md'
    memory_path = memories / 'MEMORY.md'
    initial_user = OLD + '\n§\n' + OTHER
    if scenario in ('partial_verified_merge', 'partial_new_add', 'partial_mixed_batch', 'partial_duplicate_stale'):
        initial_user += '\n§\n' + OLD
    user_path.write_text(initial_user, encoding='utf-8')
    memory_path.write_text('Synthetic system fact.', encoding='utf-8')
    initial_memory = memory_path.read_bytes()
    files = {}
    for name in ('MEMORY.md', 'USER.md'):
        data = (memories / name).read_bytes()
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'entries': data.decode().split('\n§\n')}
    (report_dir / 'baseline.json').write_text(json.dumps({'profile_home': str(home), 'files': files}))
    monkeypatch.setattr(memory_audit_policy, 'BASELINE_SHA256', hashlib.sha256((report_dir / 'baseline.json').read_bytes()).hexdigest())

    db = open_persistence(profile_scope(home=home))
    db.sessions.create(NewSession('interaction', source='desktop'))
    if scenario.startswith('complete_'):
        from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
        from gateway.session_display import submission_turn_scope
        from hermes_state_runtime import admit_session_input, begin_runtime_epoch
        content = ('Synthetic a\u200bb preference.' if scenario == 'complete_invisible_unicode'
                   else 'Synthetic preference evidence.')
        request_id = f'fixture-{scenario}'
        epoch = begin_runtime_epoch(db, instance_id='memory-audit-fixture')
        admission = admit_session_input(
            db, epoch=epoch, principal_id='human:fixture', session_id='interaction',
            request_id=request_id, payload={'text': content})
        prepared = SimpleNamespace(
            persist_user_message=None, message_text=content, persist_user_timestamp=None,
            persist_user_display_kind=None, persistence_owner=None)
        event = SimpleNamespace(message_id=admission['admission_id'], internal=False, metadata={}, text=content)
        with submission_turn_scope(admission['admission_id'], request_id):
            message = GatewayTurnPersistenceMixin._hmwa_user_transcript_entry(
                event, prepared, _audit_window_timestamp())
        _append_batch(raw_holder(db), 'interaction', [message])
    elif scenario == 'partial_empty':
        pass
    elif scenario == 'partial_unknown_bot':
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': 'Synthetic opaque bot-origin evidence',
             'display_kind': 'hidden', 'timestamp': _audit_window_timestamp()},
        ])
    elif scenario == 'partial_hidden_notice':
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': '[Background process synthetic]\nUnverified human preference',
             'display_kind': 'hidden', 'timestamp': _audit_window_timestamp()},
        ])
    elif scenario == 'partial_api_sidecar':
        from agent.turn_facade_lease import carry_unadmitted_user_message
        messages = {'interrupted': True, 'messages': []}
        carry_unadmitted_user_message(messages, 'unverified request', 'visible text',
                                      timestamp=_audit_window_timestamp(), display_kind=None,
                                      display_metadata=None, platform_id=None)
        _append_batch(raw_holder(db), 'interaction', messages['messages'])
    elif scenario == 'partial_absorbed_content':
        now = _audit_window_timestamp()
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': 'Synthetic first request', 'message_uid': 'same', 'timestamp': now},
            {'role': 'user', 'content': 'Synthetic first request', 'message_uid': 'same', 'timestamp': now,
             '_absorbed_message_uids': ['missing-synthetic-user']},
        ])
    elif scenario == 'partial_summary_tail':
        from agent.context_compressor import ContextCompressor
        now = _audit_window_timestamp()
        compressor = object.__new__(ContextCompressor)
        compressor.quiet_mode = True
        compressor._summary_has_user_turn = True
        original = {'role': 'user', 'content': 'Original synthetic request.', 'message_uid': 'prior', 'timestamp': now}
        carrier = dict(original)
        compressor._merge_summary_into_tail_row(carrier, 'Synthetic summary', 'user', False)
        inflight = {'role': 'user', 'content': 'Unwitnessed synthetic request.', 'message_uid': 'later', 'timestamp': now}
        _append_batch(raw_holder(db), 'interaction', [original, *compressor._reappend_inflight_user_task([carrier], inflight)])
    elif scenario.startswith('partial_'):
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': '[CRON ambiguous fixture]', 'timestamp': _audit_window_timestamp()},
        ])
    elif scenario == 'complete_invisible_unicode':
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': 'Synthetic a\u200bb preference.', 'timestamp': _audit_window_timestamp()},
        ])
    else:
        _append_batch(raw_holder(db), 'interaction', [
            {'role': 'user', 'content': 'Synthetic preference evidence.', 'timestamp': _audit_window_timestamp()},
        ])
    monkeypatch.setattr(memory_audit_policy, 'PROFILE_HOME', home)
    if scenario == 'invalid_publication':
        monkeypatch.setattr(memory_audit_policy, 'publication_binding', lambda data, current_home: None)
    session_id, job_id, fire_id = 'synthetic-cron', '562b2c8515ee', 'synthetic-fire'
    db.sessions.create(NewSession(session_id, source='cron'))

    collector_target = scripts / memory_audit_policy.SCRIPT_NAME
    collector_file = 'scripts/' + memory_audit_policy.SCRIPT_NAME
    collector_target.write_bytes((CANDIDATE / collector_file).read_bytes())
    report_root = tmp_path / 'weekly-reports'
    report_root.mkdir()
    monkeypatch.setattr(memory_audit_policy, 'REPORT_ROOT', report_root)
    from cron import memory_audit_artifacts as artifacts
    catalog_receipt = report_dir / 'catalog/generation.json'
    monkeypatch.setattr(artifacts, 'CATALOG_RECEIPT', catalog_receipt)
    helper = Path(artifacts.__file__).resolve()
    publisher = scripts / 'publish-builtin-memory-report.py'
    publisher.write_text(
            'import argparse,datetime,hashlib,json,pathlib\n'
            "p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--run-time');a=p.parse_args()\n"
            "source=pathlib.Path(a.source); stamp=datetime.datetime.fromisoformat(a.run_time); day=stamp.date()\n"
            "start=day-datetime.timedelta(days=(day.weekday()-5)%7);end=start+datetime.timedelta(days=6)\n"
            f"root=pathlib.Path({str(report_root)!r})\n"
            "dest=root/f'{start}_{end}'/str(day)/f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'\n"
            "dest.parent.mkdir(parents=True);dest.write_bytes(__memory_audit_source_bytes__);digest=hashlib.sha256(dest.read_bytes()).hexdigest()\n"
            "r={'html':str(dest),'html_sha256':digest,'report_readback_verified':True,"
            "'creation_baseline':__memory_audit_creation_baseline__,"
            f"'library_indexed': {scenario != 'partial_report_bad_receipt'}, "
            "'library_document_id':'synthetic','library_preview_sha256':digest,"
            "'source':str(source),'source_sha256':hashlib.sha256(__memory_audit_source_bytes__).hexdigest(),"
            "'run_time':stamp.isoformat(),'catalog_exit_code':0}\n"
            f"generation=pathlib.Path({str(catalog_receipt)!r})\n"
            "generation.parent.mkdir(parents=True,exist_ok=True);index=root/'index.html';index.write_text('<html>catalog fixture</html>')\n"
            "generation.write_text(json.dumps({'output_html':str(index),'html_sha256':hashlib.sha256(index.read_bytes()).hexdigest(),'sources':{}}))\n"
            "artifacts=__memory_audit_artifacts__;r.update(artifacts.catalog_readback(root,generation,dest,artifacts.raw_manifest(__memory_audit_dependencies__),artifacts.input_manifest(artifacts.snapshot_catalog_inputs(root))))\n"
            "(source.parent/f'publication-{stamp:%Y%m%d-%H%M%S}.json').write_text(json.dumps(r))\n"
            "print(json.dumps(r))\n",
        encoding='utf-8')
    if scenario == 'collection_error_publisher_failure':
        publisher.write_text('raise SystemExit(7)\n', encoding='utf-8')
    assert hashlib.sha256(collector_target.read_bytes()).hexdigest() == memory_audit_policy.COLLECTOR_SHA256
    interpreter_path, interpreter_sha256, interpreter_error = scheduler.resolve_script_interpreter_identity(
        {'interpreter': None}, memory_audit_policy.SCRIPT_NAME)
    assert interpreter_error is None
    monkeypatch.setattr(
        memory_audit_policy, 'approved_interpreter_identity',
        lambda: (interpreter_path, interpreter_sha256))
    monkeypatch.setattr(
        memory_audit_policy, 'PUBLISHER_SHA256', hashlib.sha256(publisher.read_bytes()).hexdigest())
    monkeypatch.setattr(memory_audit_policy, 'PUBLISHER_DEPENDENCY_SHA256', {
        str(helper): hashlib.sha256(helper.read_bytes()).hexdigest()})
    store = MemoryStore()
    store.load_from_disk()
    recorded = {}
    original_class = fixtures.AIAgent
    original_run_script = scheduler._run_job_script_with_claim_heartbeat

    def run_script(*args, **kwargs):
        result = original_run_script(*args, **kwargs)
        if scenario in ('collection_error', 'collection_error_publisher_failure'):
            result = (False, '{"schema":"builtin-memory-audit-input.v1","collection_status":"error"}')
        elif scenario == 'early_empty':
            result = (True, '')
        elif scenario == 'early_wake_false':
            result = (True, '{"wakeAgent": false}')
        recorded['script_result'] = result
        return result

    def make_agent(*args, **kwargs):
        definitions = [{'type': 'function', 'function': copy.deepcopy(memory_tool.MEMORY_SCHEMA)}]
        if scenario not in ('partial_report_missing', 'invalid_publication', 'collection_error', 'collection_error_publisher_failure'):
            definitions += fixtures._make_tool_defs('write_file', 'terminal')
        with (patch.object(fixtures, '_make_tool_defs', return_value=definitions),
              patch.object(fixtures, 'AIAgent', side_effect=lambda *a, **kw: original_class(
                  *a, **kw, session_db=db, persistence=db, session_id=session_id))):
            agent = fixtures._make_agent('memory', 'write_file', 'terminal', max_iterations=5, platform='cron')
        agent._memory_store = store
        agent._memory_enabled = True
        agent._user_profile_enabled = True
        agent._memory_manager = None
        agent._disable_streaming = True
        args = {'target': 'user', 'operations': [{'action': 'patch', 'old_text': OLD,
                 'content': NEW, 'matched_entry': OLD}]}
        if scenario == 'complete_legacy_patch':
            args = {'target': 'user', 'action': 'patch', 'old_text': OLD, 'content': NEW}
        if scenario == 'complete_unpinned_patch':
            args['operations'] = [{'action': 'patch', 'old_text': OLD, 'content': NEW}]
        if scenario == 'partial_verified_merge':
            args['operations'] = [{'action': 'add', 'content': OLD}]
        if scenario in ('complete_add_stale', 'partial_new_add'):
            args['operations'] = [{'action': 'add', 'content': NEW}]
        if scenario == 'partial_mixed_batch':
            args['operations'] = [{'action': 'add', 'content': OLD}, {'action': 'add', 'content': NEW}]
        if scenario == 'partial_duplicate_stale':
            args['operations'] = [{'action': 'add', 'content': OLD}]
        first = fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
            fixtures._mock_tool_call('memory', json.dumps(args), 'call-memory')])
        second = fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
            fixtures._mock_tool_call('memory', json.dumps({'target': 'memory', 'operations': [
                {'action': 'add', 'content': 'Another synthetic system fact.'}]}), 'call-memory-second')])
        final = fixtures._mock_response(content='synthetic run finished', finish_reason='stop')
        def model_response(**kwargs):
            recorded.setdefault('model_requests', []).append(kwargs)
            if len(recorded['model_requests']) == 1:
                if scenario in ('complete_stale', 'complete_add_stale'):
                    user_path.write_text(OLD + ' Later independent condition.\n§\n' + OTHER, encoding='utf-8')
                if scenario == 'complete_other_stale':
                    memory_path.write_text('Synthetic system fact.\n§\nConcurrent other update.', encoding='utf-8')
                if scenario == 'partial_duplicate_stale':
                    user_path.write_text(initial_user + '\n§\nConcurrent update.', encoding='utf-8')
                return first
            if scenario == 'complete_both_success' and len(recorded['model_requests']) == 2:
                return second
            if scenario not in ('partial_report_missing', 'invalid_publication', 'collection_error', 'collection_error_publisher_failure'):
                policy = agent._memory_audit_policy
                write_round = 3 if scenario == 'complete_both_success' else 2
                if len(recorded['model_requests']) == write_round:
                    return fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
                        fixtures._mock_tool_call('write_file', json.dumps({
                            'path': str(policy.report_source),
                            'content': '# Synthetic report\nMEMORY.md and USER.md checked.',
                        }), 'call-write-report')])
                if len(recorded['model_requests']) == write_round + 1:
                    if scenario == 'partial_external_before_publish':
                        import threading
                        results = []
                        writer = threading.Thread(target=lambda: results.append(
                            MemoryStore().add('user', 'Concurrent independent preference.')))
                        writer.start()
                        writer.join(timeout=5)
                        assert not writer.is_alive()
                        assert results[0].get('success') is True, results
                    import shlex, sys
                    command = shlex.join([sys.executable, str(policy.publisher), '--source',
                                          str(policy.report_source), '--run-time', policy.run_time])
                    publish_args: dict[str, object] = {'command': command}
                    if scenario == 'partial_report_valid':
                        publish_args['timeout'] = 600
                    return fixtures._mock_response(content='', finish_reason='tool_calls', tool_calls=[
                        fixtures._mock_tool_call('terminal', json.dumps(publish_args), 'call-publish-report')])
            return final
        agent.client.chat.completions.create.side_effect = model_response
        recorded['agent'] = agent
        return agent

    maintenance_prompt = 'synthetic memory audit prompt'
    monkeypatch.setattr(memory_audit_policy, 'PROMPT_SHA256', hashlib.sha256(maintenance_prompt.encode()).hexdigest())
    job = {'id': job_id, 'name': 'builtin-memory-daily-maintenance-trial', 'prompt': maintenance_prompt,
           'script': memory_audit_policy.SCRIPT_NAME, 'interpreter': None,
           'schedule_display': '15 3 * * *', 'enabled_toolsets': ['memory', 'file', 'terminal', 'no_mcp'],
           'deliver': 'local', 'no_agent': False, 'context_from': ['self'], 'timezone': 'Asia/Shanghai',
           'workdir': str(home / 'reports/builtin-memory-audit-trial-20261110'),
           'schedule': {'kind': 'cron', 'expr': '15 3 * * *', 'display': '15 3 * * *'}}
    owner = SimpleNamespace(db=db)
    build_prompt = scheduler._build_job_prompt

    def build_prompt_with_missing_output(*args, **kwargs):
        prompt = build_prompt(*args, **kwargs)
        if scenario == 'complete_prompt_missing' and prompt is not None:
            output = recorded['script_result'][1]
            assert output in prompt
            return prompt.replace(output, '[collector output removed]', 1)
        return prompt

    token = session_cron._execution.set((owner, session_id, job_id, fire_id))
    try:
        with (patch.object(scheduler, '_get_hermes_home', return_value=configured_home),
              patch.object(scheduler, '_run_job_script_with_claim_heartbeat', side_effect=run_script),
              patch.object(scheduler, '_build_job_prompt', side_effect=build_prompt_with_missing_output),
              patch.object(scheduler, 'note_cron_skipped'),
              patch.object(scheduler, '_construct_cron_agent', side_effect=make_agent),
              patch.object(scheduler, '_open_cron_session_db',
                           side_effect=lambda job=None: open_persistence(profile_scope(home=home))),
              patch.object(scheduler, '_guard_job_credential_exfil'),
              patch.object(scheduler, '_resolve_cron_agent_setup', return_value=scheduler._CronAgentSetup(
                    model='test/model', runtime={})),
              patch.object(hermes_state_registry, 'release_or_close'),
              patch.object(run_agent, 'AIAgent', original_class),
              patch('hermes_cli.plugins.discover_plugins')):
            success, output, final, error = scheduler.run_job(job, execution_id=fire_id)
    finally:
        session_cron._execution.reset(token)

    if scenario in ('early_empty', 'early_wake_false'):
        assert not success, (final, error)
        assert 'memory_audit_collection_error' in (error or '')
        assert not recorded.get('model_requests')
        assert user_path.read_text() == initial_user
        assert memory_path.read_bytes() == initial_memory
        db.close()
        return

    if scenario == 'partial_external_before_publish':
        assert not success, (final, error)
        assert error == 'memory_audit_report_not_verified'
        assert 'Concurrent independent preference.' in user_path.read_text()
        assert not list(report_root.glob('*/*/记忆维护-*.html'))
        db.close()
        return

    if scenario in ('invalid_publication', 'collection_error', 'collection_error_publisher_failure',
                    'complete_prompt_missing'):
        assert not success, (final, error)
        expected_error = ('memory_audit_invalid_publication_binding' if scenario == 'invalid_publication'
                          else 'memory_audit_collection_error')
        assert expected_error in (error or '')
        assert final.startswith('[CRON_FAILURE]\n')
        reports = list(report_dir.glob('maintenance-*.md'))
        assert len(reports) == 1
        html_reports = list(report_root.glob('*/*/记忆维护-*.html'))
        if scenario == 'collection_error_publisher_failure':
            assert html_reports == []
            assert 'failure_report_publish_failed' in error
        else:
            assert len(html_reports) == 1
            assert str(html_reports[0]) in final
        assert '未修改' in reports[0].read_text(encoding='utf-8')
        assert user_path.read_text() == initial_user
        assert memory_path.read_bytes() == initial_memory
        assert not recorded.get('model_requests')
        db.close()
        return

    if scenario in ('partial_report_missing', 'partial_report_bad_receipt'):
        assert recorded.get('model_requests')
        if scenario == 'partial_report_missing':
            assert not list(report_dir.glob('maintenance-*.md'))
        else:
            assert len(list(report_dir.glob('publication-*.json'))) == 1
        assert user_path.read_text() == initial_user
        assert memory_path.read_bytes() == initial_memory
        assert not success, (success, final, error)
        assert error == 'memory_audit_report_not_verified'
        db.close()
        return

    if scenario == 'partial_report_valid':
        assert success, (success, final, error)
        assert user_path.read_text() == initial_user
        assert memory_path.read_bytes() == initial_memory
        receipts = list(report_dir.glob('publication-*.json'))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text())
        assert receipt['library_indexed'] is True
        assert hashlib.sha256(Path(receipt['html']).read_bytes()).hexdigest() == receipt['html_sha256']
        assert receipt['source_sha256'] == hashlib.sha256(Path(receipt['source']).read_bytes()).hexdigest()
        db.close()
        return

    if scenario in ('complete_stale', 'complete_add_stale', 'complete_other_stale', 'partial_duplicate_stale'):
        assert not success, (final, error, output[-2000:])
        assert error == 'memory_audit_report_not_verified'
        db.close()
        return
    else:
        assert success, (final, error, recorded.get('script_result'), output[-2000:])
    assert final == 'synthetic run finished'
    expected_status = 'partial' if scenario.startswith('partial_') else 'complete'
    assert f'"collection_status": "{expected_status}"' in recorded['script_result'][1], repr(recorded['script_result'])
    tool_rows = [r for r in transcript_rows(db, session_id, TranscriptRead(view=TranscriptView.ALL))
                 if r.get('role') == 'tool' and r.get('tool_name') == 'memory']
    assert len(tool_rows) == (2 if scenario == 'complete_both_success' else 1), tool_rows
    response = json.loads(tool_rows[0]['content'])
    # health: allow HX011 -- outcome-specific assertions keep each conservative refusal explicit
    if scenario == 'complete_stale':
        assert response['success'] is False, response
        assert response['error'] == 'memory_audit_snapshot_or_run_changed'
        assert user_path.read_text() == OLD + ' Later independent condition.\n§\n' + OTHER
    elif scenario == 'complete_add_stale':
        assert response['success'] is False, response
        assert user_path.read_text() == OLD + ' Later independent condition.\n§\n' + OTHER
    elif scenario in ('complete_legacy_patch', 'complete_unpinned_patch'):
        assert response.get('success') is False or 'error' in response, response
        assert response['error'] == 'memory_audit_invalid_or_unpinned_request'
        assert user_path.read_text() == initial_user
    elif scenario == 'complete_other_stale':
        assert response.get('success') is False or 'error' in response, response
        assert user_path.read_text() == initial_user
    elif scenario in ('partial_forced_write', 'partial_report_binding', 'partial_report_binding_symlink_home', 'partial_unknown_bot', 'partial_empty',
                      'partial_hidden_notice', 'partial_summary_tail',
                      'partial_api_sidecar', 'partial_absorbed_content'):
        assert response.get('success') is False or 'error' in response, response
        assert user_path.read_text() == OLD + '\n§\n' + OTHER
    elif scenario == 'partial_verified_merge':
        assert response['success'] is True, response
        assert user_path.read_text() == OLD + '\n§\n' + OTHER
    elif scenario in ('partial_new_add', 'partial_mixed_batch', 'partial_duplicate_stale'):
        assert response.get('success') is False or 'error' in response, response
        expected = initial_user + ('\n§\nConcurrent update.' if scenario == 'partial_duplicate_stale' else '')
        assert user_path.read_text() == expected
    else:
        assert response['success'] is True, response
        assert user_path.read_text() == NEW + '\n§\n' + OTHER
    if scenario == 'complete_both_success':
        assert json.loads(tool_rows[1]['content'])['success'] is True
        assert memory_path.read_text() == 'Synthetic system fact.\n§\nAnother synthetic system fact.'
    elif scenario == 'complete_other_stale':
        assert memory_path.read_text() == 'Synthetic system fact.\n§\nConcurrent other update.'
    else:
        assert memory_path.read_bytes() == initial_memory
    assert len(recorded['model_requests']) >= 2
    if scenario == 'complete_invisible_unicode':
        output = recorded['script_result'][1]
        assert '\u200b' in output
        escaped = memory_audit_policy.prompt_output(output)
        assert json.loads(escaped) == json.loads(output)
        messages = recorded['model_requests'][0]['messages']
        assert any(escaped in str(message.get('content', '')) for message in messages)
    if scenario in ('partial_unknown_bot', 'partial_empty'):
        assert recorded['script_result'][0] is True
        payload = json.loads(recorded['script_result'][1])
        history = payload['history']
        assert payload['collection_status'] == 'partial'
        assert history['complete_within_declared_scope'] is False
        assert history['messages'] == [] and history['included_messages'] == 0
        assert history['uncertain_source_count'] == (1 if scenario == 'partial_unknown_bot' else 0)
        assert 'Synthetic opaque bot-origin evidence' not in recorded['script_result'][1]
        assert 'Synthetic opaque bot-origin evidence' not in json.dumps(recorded['model_requests'])
    if scenario == 'complete_success':
        memory_schemas = [tool['function'] for tool in recorded['model_requests'][0].get('tools', [])
                          if tool.get('function', {}).get('name') == 'memory']
        assert len(memory_schemas) == 1
        properties = memory_schemas[0]['parameters']['properties']['operations']['items']['properties']
        assert 'matched_entry' in properties
    if scenario in ('partial_report_binding', 'partial_report_binding_symlink_home'):
        assert recorded['script_result'][0] is True
        payload = json.loads(recorded['script_result'][1])
        from datetime import datetime
        stamp = datetime.fromisoformat(payload['observed_at'])
        day = stamp.astimezone(ZoneInfo('Asia/Shanghai')).date()
        week_start = day - timedelta(days=(day.weekday() - 5) % 7)
        week_end = week_start + timedelta(days=6)
        policy = recorded['agent']._memory_audit_policy
        assert policy.report_source == report_dir / f'maintenance-{stamp:%Y%m%d-%H%M%S}.md'
        assert policy.publisher == scripts / 'publish-builtin-memory-report.py'
        assert policy.run_time == stamp.isoformat()
        assert policy.read_paths == {
            memory_path, user_path, Path(payload['verified_backup']) / 'manifest.json', policy.report_source,
            report_root / f'{week_start}_{week_end}' / str(day) / f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html',
            report_dir / f'publication-{stamp:%Y%m%d-%H%M%S}.json',
        }
    db.close()
