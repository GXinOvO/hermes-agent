import json
import logging
import builtins
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('phase', ['journal_save', 'seal_import', 'seal_validate', 'seal_writeback'])
@pytest.mark.parametrize('kind', ['agent', 'script'])
def test_completed_result_failure_reports_stage_without_delivery(tmp_path, monkeypatch, caplog, phase, kind):
    from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority, scheduler_results
    from cron.scheduler_authority import journal_path

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if kind == 'script':
        scripts = tmp_path / 'scripts'
        scripts.mkdir()
        (scripts / 'report.py').write_text(
            'from pathlib import Path\n'
            f'p=Path({str(tmp_path / "script-ran")!r})\n'
            'with p.open("a") as f: f.write("once\\n")\n'
            'print("answer")\n')
        job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                              schedule='every 1h', deliver='bot-chat')
    else:
        job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
        monkeypatch.setattr(scheduler, 'run_job', lambda *a, **kw: (True, 'output', 'answer', None))
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    sent = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: sent.append((a, kw)))
    real_save = scheduler_results._save
    real_validate = scheduler_results.validate_binding
    real_import = builtins.__import__
    secret = 'private-exception-detail'
    injected = []

    def injected_save(path, record):
        has_result = 'result' in record
        has_seal = '_sealed_bot_result_v1' in record.get('job', {})
        if has_result and ((phase == 'journal_save' and not has_seal)
                           or (phase == 'seal_writeback' and has_seal)):
            injected.append(phase)
            raise OSError(secret)
        return real_save(path, record)

    def injected_validate(*args):
        injected.append(phase)
        raise ValueError(secret)

    def injected_import(name, *args, **kwargs):
        caller = args[0] if args else {}
        if name == 'cron.sealed_bot_result' and caller.get('__name__') == 'cron.scheduler_results':
            injected.append(phase)
            raise ImportError(secret)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(scheduler_results, '_save', injected_save)
    if phase == 'seal_validate':
        monkeypatch.setattr(scheduler_results, 'validate_binding', injected_validate)
    if phase == 'seal_import':
        monkeypatch.setattr(builtins, '__import__', injected_import)
    caplog.set_level(logging.ERROR)

    assert scheduler._run_one_job_body(job) is False
    assert injected == [phase]
    execution = executions.get_execution(execution_id)
    expected_error = ('Cron script finished but its result receipt could not be saved'
                      if kind == 'script' else 'Cron completed result receipt could not be saved')
    assert execution['status'] == 'unknown'
    assert execution['error'] == expected_error
    paused = jobs.get_job(job['id'])
    assert paused['state'] == 'paused'
    assert paused['paused_reason'] == expected_error
    assert delivery_queue.get_status(execution_id) is None
    assert sent == []
    if kind == 'script':
        assert (tmp_path / 'script-ran').read_text().splitlines() == ['once']
    if phase != 'journal_save':
        record = json.loads(journal_path(job['id'], execution_id).read_text())
        assert len(record['result']) == 4
        assert '_sealed_bot_result_v1' not in record['job']
    expected = 'seal' if phase == 'seal_validate' else phase
    assert any(f'stage={expected} ' in record.message and f'job={job["id"]} ' in record.message
               and f'execution={execution_id} ' in record.message for record in caplog.records)
    error_type = {'seal_validate': 'ValueError', 'seal_import': 'ImportError'}.get(phase, 'OSError')
    assert error_type in caplog.text
    assert secret not in caplog.text

    monkeypatch.setattr(scheduler_results, '_save', real_save)
    monkeypatch.setattr(scheduler_results, 'validate_binding', real_validate)
    monkeypatch.setattr(builtins, '__import__', real_import)
    scheduler_authority.reconcile_pending(allow_connect=False)
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert executions.get_execution(execution_id)['status'] == 'unknown'
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert delivery_queue.get_status(execution_id) is None
    assert sent == []
    if kind == 'script':
        assert (tmp_path / 'script-ran').read_text().splitlines() == ['once']
    if phase != 'journal_save':
        record = json.loads(journal_path(job['id'], execution_id).read_text())
        assert record['recovery_refused']['status'] == 'missing_sealed_provenance'


@pytest.mark.parametrize('phase', ['journal_save', 'seal_writeback'])
@pytest.mark.parametrize('kind', ['agent', 'script'])
def test_committed_write_then_error_recovers_only_proven_sealed_result(tmp_path, monkeypatch, caplog, phase, kind):
    from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority, scheduler_results
    from cron.scheduler_authority import journal_path

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    marker = tmp_path / 'script-ran'
    if kind == 'script':
        scripts = tmp_path / 'scripts'
        scripts.mkdir()
        (scripts / 'report.py').write_text(
            'from pathlib import Path\n'
            f'p=Path({str(marker)!r})\n'
            'with p.open("a") as f: f.write("once\\n")\n'
            'print("answer")\n')
        job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                              schedule='every 1h', deliver='bot-chat')
    else:
        job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
        def run(*args, **kwargs):
            calls.append('once')
            return True, 'output', 'answer', None
        monkeypatch.setattr(scheduler, 'run_job', run)
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    real_save = scheduler_results._save
    sent = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: sent.append((a, kw)))

    def save_then_fail(path, record):
        real_save(path, record)
        has_result = 'result' in record
        has_seal = '_sealed_bot_result_v1' in record.get('job', {})
        if has_result and ((phase == 'journal_save' and not has_seal)
                           or (phase == 'seal_writeback' and has_seal)):
            raise OSError('post-write-failure')

    monkeypatch.setattr(scheduler_results, '_save', save_then_fail)
    caplog.set_level(logging.ERROR)
    assert scheduler._run_one_job_body(job) is False
    assert f'stage={phase}' in caplog.text
    assert executions.get_execution(execution_id)['status'] == 'unknown'
    assert delivery_queue.get_status(execution_id) is None
    journal = journal_path(job['id'], execution_id)
    stored = json.loads(journal.read_text())
    assert bool(stored['job'].get('_sealed_bot_result_v1')) is (phase == 'seal_writeback')
    monkeypatch.setattr(scheduler_results, '_save', real_save)
    scheduler_authority.reconcile_pending(allow_connect=False)
    scheduler_authority.reconcile_pending(allow_connect=False)
    if phase == 'journal_save':
        assert executions.get_execution(execution_id)['status'] == 'unknown'
        assert jobs.get_job(job['id'])['state'] == 'paused'
        assert delivery_queue.get_status(execution_id) is None
        assert json.loads(journal.read_text())['recovery_refused']['status'] == 'missing_sealed_provenance'
    else:
        assert executions.get_execution(execution_id)['status'] == 'completed'
        assert jobs.get_job(job['id'])['state'] == 'scheduled'
        row = delivery_queue.get_status(execution_id)
        assert row is not None and row['status'] == 'pending'
        assert json.loads(row['job_json'])['_sealed_bot_result_v1']['execution_id'] == execution_id
    assert sent == []
    assert (marker.read_text().splitlines() if kind == 'script' else calls) == ['once']


def test_seal_path_resolution_failure_is_not_labeled_writeback(tmp_path, monkeypatch, caplog):
    from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    monkeypatch.setattr(scheduler, 'run_job', lambda *a, **kw: (True, 'output', 'answer', None))
    original = scheduler_authority.journal_path
    calls = []

    def path(job_id, request_id):
        calls.append((job_id, request_id))
        if len(calls) == 3:
            raise OSError('synthetic path failure')
        return original(job_id, request_id)

    monkeypatch.setattr(scheduler_authority, 'journal_path', path)
    caplog.set_level(logging.ERROR)
    assert scheduler._run_one_job_body(job) is False
    assert len(calls) >= 3
    assert executions.get_execution(execution_id)['status'] == 'unknown'
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert delivery_queue.get_status(execution_id) is None
    assert any(f'stage=seal job={job["id"]} execution={execution_id} error_type=OSError' in record.message
               for record in caplog.records)
    assert 'stage=seal_writeback' not in caplog.text
    assert 'synthetic path failure' not in caplog.text


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('phase', ['journal_save', 'seal_writeback'])
def test_two_recovery_processes_after_committed_failure(tmp_path, monkeypatch, phase):
    from cron import delivery_queue, executions, jobs
    from cron.scheduler_authority import journal_path

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    marker = tmp_path / 'script-ran'
    (scripts / 'report.py').write_text(
        'from pathlib import Path\n'
        f'p=Path({str(marker)!r})\n'
        'with p.open("a") as f: f.write("once\\n")\n'
        'print("answer")\n')
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='bot-chat')
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    assert executions.mark_execution_handoff_pending(execution_id)
    env = dict(os.environ, HERMES_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    producer_code = '''
        import json, os, sys
        from cron import executions, scheduler, scheduler_results
        request = json.load(sys.stdin)
        eid = request['job']['execution_id']
        assert executions.adopt_claimed_execution(eid)
        os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = eid
        save = scheduler_results._save
        def fail_after_write(path, record):
            save(path, record)
            has_result = 'result' in record
            has_seal = '_sealed_bot_result_v1' in record.get('job', {})
            if has_result and ((request['phase'] == 'journal_save' and not has_seal)
                               or (request['phase'] == 'seal_writeback' and has_seal)):
                raise OSError('synthetic post-write failure')
        scheduler_results._save = fail_after_write
        assert scheduler._run_one_job_body(request['job']) is False
        assert executions.get_execution(eid)['status'] == 'unknown'
        print('producer_unknown')
        '''
    producer = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(producer_code)],
                              input=json.dumps({'job': job, 'phase': phase}), env=env,
                              text=True, capture_output=True, timeout=30, check=False)
    assert producer.returncode == 0, (producer.stdout, producer.stderr)
    assert 'producer_unknown' in producer.stdout
    assert marker.read_text().splitlines() == ['once']
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert delivery_queue.get_status(execution_id) is None
    journal = journal_path(job['id'], execution_id)
    stored = json.loads(journal.read_text())
    assert bool(stored['job'].get('_sealed_bot_result_v1')) is (phase == 'seal_writeback')

    reader_code = '''
        import fcntl, json, socket, sys
        from pathlib import Path
        from cron import jobs, scheduler, scheduler_authority
        request = json.load(sys.stdin)
        connection = socket.create_connection(tuple(request['address']), timeout=15)
        real_lock = jobs._acquire_flock
        def lock(fd, timeout):
            if Path(fd.name).name.startswith('.fire-'):
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    connection.sendall(b'L')
                else:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                    raise AssertionError('expected locked fire fence')
            return real_lock(fd, timeout)
        jobs._acquire_flock = lock
        real_save = scheduler.save_job_output
        def save(*args, **kwargs):
            with Path(request['save_count']).open('a') as out:
                out.write('saved\\n')
            return real_save(*args, **kwargs)
        scheduler.save_job_output = save
        scheduler_authority.reconcile_pending(allow_connect=False)
        connection.close()
        '''
    save_count = tmp_path / 'saved-output-calls'
    readers = []
    connections = []
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(2)
        server.settimeout(20)
        try:
            with jobs._fire_job_lock(job['id']) as acquired:
                assert acquired
                for _ in range(2):
                    child = subprocess.Popen([sys.executable, '-B', '-c', textwrap.dedent(reader_code)],
                                             env=env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    child.stdin.write(json.dumps({'address': server.getsockname(),
                                                  'save_count': str(save_count)}))
                    child.stdin.close()
                    child.stdin = None
                    readers.append(child)
                for _ in readers:
                    connection, _ = server.accept()
                    connection.settimeout(20)
                    connections.append(connection)
                    assert connection.recv(1) == b'L'
                journal_before = journal.read_bytes()
                timeout_code = '''
                    from pathlib import Path
                    from cron import jobs, scheduler_authority
                    jobs._JOBS_LOCK_TIMEOUT_SECONDS = 0.2
                    real_lock = jobs._acquire_flock
                    def lock(fd, timeout):
                        acquired = real_lock(fd, timeout)
                        if Path(fd.name).name.startswith('.fire-'):
                            print('fire_acquired=' + str(acquired), flush=True)
                        return acquired
                    jobs._acquire_flock = lock
                    scheduler_authority.reconcile_pending(allow_connect=False)
                    '''
                refused = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(timeout_code)],
                                         env=env, text=True, capture_output=True, timeout=30, check=False)
                assert refused.returncode == 0, (refused.stdout, refused.stderr)
                assert 'fire_acquired=False' in refused.stdout
                assert journal.read_bytes() == journal_before
                assert executions.get_execution(execution_id)['status'] == 'unknown'
                assert delivery_queue.get_status(execution_id) is None
                assert not save_count.exists()
            for child in readers:
                stdout, stderr = child.communicate(timeout=30)
                assert child.returncode == 0, (stdout, stderr)
        finally:
            for connection in connections:
                connection.close()
            for child in readers:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    with sqlite3.connect(tmp_path / 'cron' / 'deliveries.db') as db:
        rows = db.execute('SELECT execution_id,status FROM deliveries').fetchall()
    assert marker.read_text().splitlines() == ['once']
    if phase == 'journal_save':
        assert rows == [] and not save_count.exists()
        assert executions.get_execution(execution_id)['status'] == 'unknown'
        assert jobs.get_job(job['id'])['state'] == 'paused'
        assert json.loads(journal.read_text())['recovery_refused']['status'] == 'missing_sealed_provenance'
    else:
        assert rows == [(execution_id, 'pending')]
        assert save_count.read_text().splitlines() == ['saved']
        assert executions.get_execution(execution_id)['status'] == 'completed'
        assert jobs.get_job(job['id'])['state'] == 'scheduled'


@pytest.mark.parametrize('kind', ['agent', 'script'])
@pytest.mark.parametrize('phase', ['journal_save', 'seal_import', 'seal_validate', 'seal_writeback'])
def test_diagnostic_handler_failure_does_not_change_uncertain_result(tmp_path, monkeypatch, kind, phase):
    from cron import delivery_queue, executions, jobs, scheduler, scheduler_results

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if kind == 'script':
        scripts = tmp_path / 'scripts'
        scripts.mkdir()
        (scripts / 'report.py').write_text('print("answer")\n')
        job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                              schedule='every 1h', deliver='bot-chat')
    else:
        job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
        monkeypatch.setattr(scheduler, 'run_job', lambda *a, **kw: (True, 'output', 'answer', None))
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    original_save = scheduler_results._save
    original_import = builtins.__import__

    def save(path, record):
        has_result = 'result' in record
        has_seal = '_sealed_bot_result_v1' in record.get('job', {})
        if has_result and ((phase == 'journal_save' and not has_seal)
                           or (phase == 'seal_writeback' and has_seal)):
            raise OSError('injected store failure')
        return original_save(path, record)

    def fail_validation(*args):
        raise ValueError('injected validation failure')

    def fail_import(name, *args, **kwargs):
        caller = args[0] if args else {}
        if name == 'cron.sealed_bot_result' and caller.get('__name__') == 'cron.scheduler_results':
            raise ImportError('injected import failure')
        return original_import(name, *args, **kwargs)

    class FailingHandler(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith('Cron result receipt stage='):
                raise OSError('injected log handler failure')

    monkeypatch.setattr(scheduler_results, '_save', save)
    if phase == 'seal_validate':
        monkeypatch.setattr(scheduler_results, 'validate_binding', fail_validation)
    if phase == 'seal_import':
        monkeypatch.setattr(builtins, '__import__', fail_import)
    sent = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: sent.append((a, kw)))
    handler = FailingHandler()
    scheduler.logger.addHandler(handler)
    try:
        assert scheduler._run_one_job_body(job) is False
    finally:
        scheduler.logger.removeHandler(handler)
        handler.close()
        monkeypatch.setattr(builtins, '__import__', original_import)
    expected_error = ('Cron script finished but its result receipt could not be saved'
                      if kind == 'script' else 'Cron completed result receipt could not be saved')
    execution = executions.get_execution(execution_id)
    assert execution['status'] == 'unknown'
    assert execution['error'] == expected_error
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert delivery_queue.get_status(execution_id) is None
    assert sent == []


@pytest.mark.parametrize('problem', ['missing_job_id', 'handler_raises'])
def test_seal_diagnostic_never_replaces_original_exception(monkeypatch, problem):
    from cron import sealed_bot_result

    original = ValueError('original seal failure')

    def fail_seal(*args):
        raise original

    class FailingHandler(logging.Handler):
        def emit(self, record):
            raise OSError('diagnostic handler failure')

    monkeypatch.setattr(sealed_bot_result, '_freeze_completed_result', fail_seal)
    handler = FailingHandler()
    if problem == 'handler_raises':
        sealed_bot_result.logger.addHandler(handler)
    try:
        with pytest.raises(ValueError) as raised:
            sealed_bot_result.freeze_completed_result(
                {} if problem == 'missing_job_id' else {'id': 'job'}, 'execution', {})
        assert raised.value is original
    finally:
        sealed_bot_result.logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize('kind', ['agent', 'script'])
@pytest.mark.parametrize('phase', ['journal_save', 'seal_import', 'seal', 'seal_writeback'])
def test_original_exception_is_cause_even_if_diagnostic_handler_raises(tmp_path, monkeypatch, kind, phase):
    from cron import executions, jobs, scheduler, scheduler_authority, scheduler_results, sealed_bot_result

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if kind == 'script':
        scripts = tmp_path / 'scripts'
        scripts.mkdir()
        (scripts / 'report.py').write_text('print("answer")\n')
        job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                              schedule='every 1h', deliver='bot-chat')
        monkeypatch.setattr(scheduler, '_run_no_agent_job',
                            lambda *a, **kw: (True, 'output', 'answer', None))
    else:
        job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    assert executions.mark_execution_running(execution_id)
    original_error = ImportError('original') if phase == 'seal_import' else OSError('original')
    original_save = scheduler_results._save
    original_import = builtins.__import__

    def save(path, record):
        if ('result' in record
                and ((phase == 'journal_save' and '_sealed_bot_result_v1' not in record.get('job', {}))
                     or (phase == 'seal_writeback' and '_sealed_bot_result_v1' in record.get('job', {})))):
            raise original_error
        return original_save(path, record)

    def fail_seal(*args):
        raise original_error

    def fail_import(name, *args, **kwargs):
        caller = args[0] if args else {}
        if name == 'cron.sealed_bot_result' and caller.get('__name__') == 'cron.scheduler_results':
            raise original_error
        return original_import(name, *args, **kwargs)

    class FailingHandler(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith('Cron result receipt stage='):
                raise OSError('diagnostic handler failure')

    monkeypatch.setattr(scheduler_results, '_save', save)
    if phase == 'seal':
        monkeypatch.setattr(sealed_bot_result, '_freeze_completed_result', fail_seal)
    if phase == 'seal_import':
        monkeypatch.setattr(builtins, '__import__', fail_import)
    handler = FailingHandler()
    scheduler.logger.addHandler(handler)
    try:
        with pytest.raises(scheduler_authority.CronExecutionUnknown) as raised:
            if kind == 'script':
                scheduler_results.run_script_job(job, execution_id=execution_id)
            else:
                scheduler_results.record_completed_result(job, execution_id, (True, 'output', 'answer', None))
        assert raised.value.__cause__ is original_error
    finally:
        scheduler.logger.removeHandler(handler)
        handler.close()
        monkeypatch.setattr(builtins, '__import__', original_import)


def test_completed_result_normal_path_still_saves_and_delivers(tmp_path, monkeypatch):
    from cron import delivery_queue, executions, jobs, scheduler

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    job = jobs.create_job(prompt='test', schedule='every 1h', deliver='bot-chat')
    execution_id = executions.create_execution(job['id'], source='test')['id']
    job['execution_id'] = execution_id
    monkeypatch.setattr(scheduler, 'run_job', lambda *a, **kw: (True, 'output', 'answer', None))
    assert scheduler._run_one_job_body(job) is True
    assert executions.get_execution(execution_id)['status'] == 'completed'
    row = delivery_queue.get_status(execution_id)
    assert row is not None and row['status'] == 'pending'
    assert json.loads(row['job_json'])['_sealed_bot_result_v1']['execution_id'] == execution_id