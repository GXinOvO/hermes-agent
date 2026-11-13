import logging

import pytest


@pytest.mark.parametrize('phase', ['completed_tail', 'unverified'])
@pytest.mark.parametrize('fail_log', [False, True])
def test_log_failure_does_not_replace_uncertain_execution(tmp_path, monkeypatch, phase, fail_log):
    from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority, scheduler_results

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    marker = tmp_path / 'script-ran'
    (scripts / 'report.py').write_text(
        'from pathlib import Path\n'
        f'p=Path({str(marker)!r})\n'
        'with p.open("a") as f: f.write("once\\n")\n'
        'print("business result")\n')
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='bot-chat')
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    real_save_output = scheduler.save_job_output
    real_result_save = scheduler_results._save
    injected = []

    def fail_output(*args, **kwargs):
        injected.append('output')
        raise OSError('synthetic output failure')

    def fail_result(path, record):
        if 'result' in record and '_sealed_bot_result_v1' not in record.get('job', {}):
            injected.append('journal')
            raise OSError('synthetic journal failure')
        return real_result_save(path, record)

    class FailingHandler(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if fail_log and ((phase == 'completed_tail' and 'tail recovery deferred:' in text) or (
                    phase == 'unverified' and 'paused with unverified admission:' in text)):
                injected.append('log')
                raise OSError('synthetic logging failure')

    if phase == 'completed_tail':
        monkeypatch.setattr(scheduler, 'save_job_output', fail_output)
    else:
        monkeypatch.setattr(scheduler_results, '_save', fail_result)
    sent = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: sent.append((a, kw)))
    handler = FailingHandler()
    scheduler.logger.addHandler(handler)
    result = None
    escaped = None
    try:
        try:
            result = scheduler._run_one_job_body(job)
        except OSError as exc:
            escaped = exc
    finally:
        scheduler.logger.removeHandler(handler)
        handler.close()
    expected_injected = ['output'] if phase == 'completed_tail' else ['journal']
    assert injected == expected_injected + (['log'] if fail_log else [])
    row = executions.get_execution(execution_id)
    assert row['status'] == 'unknown', f"status={row['status']}, escaped={escaped}"
    assert escaped is None, f"logging exception escaped after status={row['status']}: {escaped}"
    assert result is False
    if phase == 'unverified':
        assert jobs.get_job(job['id'])['state'] == 'paused'
    assert delivery_queue.get_status(execution_id) is None
    assert sent == []
    assert marker.read_text().splitlines() == ['once']

    monkeypatch.setattr(scheduler, 'save_job_output', real_save_output)
    monkeypatch.setattr(scheduler_results, '_save', real_result_save)
    scheduler_authority.reconcile_pending(allow_connect=False)
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert marker.read_text().splitlines() == ['once']
    assert sent == []
    if phase == 'completed_tail':
        assert executions.get_execution(execution_id)['status'] == 'completed'
        queued = delivery_queue.get_status(execution_id)
        assert queued['status'] == 'pending' and queued['content'].strip() == 'business result'
    else:
        assert executions.get_execution(execution_id)['status'] == 'unknown'
        assert jobs.get_job(job['id'])['state'] == 'paused'
        assert delivery_queue.get_status(execution_id) is None