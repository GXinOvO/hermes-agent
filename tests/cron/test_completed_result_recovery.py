"""Completed script results survive a dead firer's tail without repeating its work."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority


def _python(home, code, *, input=None):
    env = dict(os.environ, HERMES_HOME=str(home))
    return subprocess.run([sys.executable, '-c', textwrap.dedent(code)], input=input,
                          env=env, text=True, capture_output=True, timeout=30, check=False)


def _crash_after_result(home, job, window):
    request = {'job': job, 'window': window}
    code = '''
        import json, os, sys
        from cron import delivery_queue, executions, scheduler
        request = json.loads(sys.stdin.read())
        execution_id = request['job']['execution_id']
        assert executions.adopt_claimed_execution(execution_id)
        os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = execution_id
        if not request['job'].get('no_agent'):
            from tests.cron.conftest import owner_execution_runner
            from cron import scheduler_authority
            scheduler_authority.run_canonical_job = owner_execution_runner()
        window = request['window']
        def crash(*args, **kwargs):
            os._exit(73)
        scheduler._deliver_result = crash if window == 'queue' else lambda *a, **kw: None
        if window == 'save':
            scheduler.save_job_output = crash
        elif window == 'queue':
            real_enqueue = delivery_queue.enqueue
            def enqueue(*args, **kwargs):
                real_enqueue(*args, **kwargs)
                crash()
            delivery_queue.enqueue = enqueue
        elif window == 'mark':
            real_mark = scheduler.mark_job_run
            def mark(*args, **kwargs):
                real_mark(*args, **kwargs)
                crash()
            scheduler.mark_job_run = mark
        scheduler._run_one_job_body(request['job'])
        raise AssertionError('crash seam was not reached')
        '''
    child = subprocess.Popen([sys.executable, '-c', textwrap.dedent(code)],
                             env=dict(os.environ, HERMES_HOME=str(home)),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = child.communicate(json.dumps(request), timeout=30)
    assert child.returncode == 73, (stdout, stderr)
    return child


def _recover_in_fresh_reader(home):
    child = _python(home, '''
        from cron.scheduler_authority import reconcile_pending
        reconcile_pending(allow_connect=False)
        ''')
    assert child.returncode == 0, (child.stdout, child.stderr)


def _mutate_terminal_row(home, job, case):
    with executions._transaction() as conn:
        if case == 'foreign-row':
            conn.execute('UPDATE executions SET job_id=? WHERE id=?', ('another-job', job['execution_id']))
        else:
            conn.execute('UPDATE executions SET status=? WHERE id=?', (case, job['execution_id']))


def _change_source(home, job, case):
    (home / 'scripts' / 'other.py').write_text('print("another source")\n')
    jobs.update_job(job['id'], {'script': 'other.py'})


def _change_script_bytes(home, job, case):
    (home / 'scripts' / 'report.py').write_text('print("changed input bytes")\n')


def _change_home_target(home, job, case):
    (home / '.env').write_text('TELEGRAM_HOME_CHANNEL=new-target\n')


def _lose_sender(home, job, case):
    child = _python(home, '''
        import os
        from cron import delivery_queue
        assert delivery_queue.claim_next()
        os._exit(75)
        ''')
    assert child.returncode == 75


@pytest.mark.parametrize('window', ['save', 'queue', 'mark'])
@pytest.mark.parametrize('kind', ['script', 'script-error', 'script-silent', 'monitor-unchanged', 'monitor-error'])
def test_script_completion_recovers_only_tail_once(tmp_path, monkeypatch, window, kind):
    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    count = home / 'script-calls'
    response = {'script': 'native script result', 'script-error': 'source failure',
                'script-silent': '', 'monitor-unchanged': 'stable',
                'monitor-error': 'source failure'}[kind]
    failed = kind.endswith('error')
    silent = kind in ('script-silent', 'monitor-unchanged')
    (home / 'scripts' / 'report.py').write_text(
        f'from pathlib import Path\np=Path({str(count)!r})\n'
        'with p.open("a") as f: f.write("executed\\n")\n'
        f'print({response!r})\nraise SystemExit({1 if failed else 0})\n')
    source = ({'prompt': 'only runs if monitor changes', 'monitor_script': 'report.py'}
              if kind.startswith('monitor') else {'prompt': None, 'script': 'report.py', 'no_agent': True})
    job = jobs.create_job(**source,
                          schedule='every 1h', deliver='telegram:original')
    if kind == 'monitor-unchanged':
        from cron.monitor import hash_monitor_output
        job = jobs.update_job(job['id'], {'monitor_state': {'last_output_hash': hash_monitor_output('stable')}})
    # Silent output never reaches enqueue: crash its tail at the nearest mark boundary.
    if silent and window == 'queue':
        window = 'mark'
    job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(job['execution_id'])
    _crash_after_result(home, job, window)
    assert count.read_text().splitlines() == ['executed']
    _recover_in_fresh_reader(home)
    row = executions.get_execution(job['execution_id'])
    assert row['status'] == ('failed' if failed else 'completed'), 'completed script result was lost before its tail could settle'
    saved = jobs.get_job(job['id'])
    assert saved['repeat']['completed'] == 1
    assert job['execution_id'] in saved['canonical_completions']
    outputs = list(jobs._job_output_dir(job['id']).glob('*.md'))
    assert outputs
    assert any(('source failure' if failed else 'silent' if kind == 'script-silent'
                else 'no_change' if silent else response) in p.read_text() for p in outputs)
    queued = delivery_queue.get_status(job['execution_id'])
    assert (queued is None) if silent else queued and queued['status'] == 'pending'
    sends = []
    assert delivery_queue.drain(lambda *args: sends.append(args)) == (0 if silent else 1)
    if not silent:
        assert sends[0][0]['deliver'] == 'telegram:original'
        assert ('source failure' in sends[0][1]) if failed else sends[0][1] == 'native script result'
    _recover_in_fresh_reader(home)
    assert delivery_queue.drain(lambda *args: sends.append(args)) == 0
    assert len(sends) == (0 if silent else 1)
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert count.read_text().splitlines() == ['executed']


@pytest.mark.parametrize('case', ['live', 'unknown', 'failed', 'cancelled', 'foreign-row',
                                 'target', 'source', 'deleted', 'send-unknown', 'source-bytes', 'home-target'])
def test_recovery_respects_owner_identity_and_terminal_send(tmp_path, monkeypatch, case):
    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = home / 'scripts' / 'report.py'
    script.write_text('print("completed payload")\n')
    job = jobs.create_job(prompt=None, script=script.name, no_agent=True,
                          schedule='every 1h', deliver='telegram:original')
    if case == 'home-target':
        (home / '.env').write_text('TELEGRAM_HOME_CHANNEL=original\n')
        job = jobs.update_job(job['id'], {'deliver': 'telegram'})
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    if case == 'live':
        code = textwrap.dedent('''
            import json, sys
            from cron import executions, scheduler
            job = json.loads(sys.stdin.readline())
            assert executions.adopt_claimed_execution(job['execution_id'])
            scheduler.run_job(job, execution_id=job['execution_id'])
            print('result persisted', flush=True)
            sys.stdin.read()
            ''')
        child = subprocess.Popen([sys.executable, '-c', code], env=dict(os.environ),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps(job) + '\n')
            child.stdin.flush()
            assert child.stdout.readline().strip() == 'result persisted'
            _recover_in_fresh_reader(home)
            assert jobs.get_job(job['id'])['last_run_at'] is None
            assert delivery_queue.get_status(eid) is None
        finally:
            child.stdin.close()
            child.wait(timeout=10)
            child.stdout.close()
    elif case == 'unknown':
        child = _python(home, '''
            import json, os, sys
            from cron import executions, scheduler
            job = json.loads(sys.stdin.read())
            assert executions.adopt_claimed_execution(job['execution_id'])
            scheduler._run_no_agent_job = lambda *a, **kw: os._exit(74)
            scheduler.run_job(job, execution_id=job['execution_id'])
            ''', input=json.dumps(job))
        assert child.returncode == 74
    else:
        _crash_after_result(home, job, 'queue' if case == 'send-unknown' else 'save')
    mutations = {
        'failed': _mutate_terminal_row, 'cancelled': _mutate_terminal_row, 'foreign-row': _mutate_terminal_row,
        'target': lambda home, job, case: jobs.update_job(job['id'], {'deliver': 'telegram:new-target'}),
        'source': _change_source, 'source-bytes': _change_script_bytes, 'home-target': _change_home_target,
        'deleted': lambda home, job, case: jobs.remove_job(job['id']),
        'send-unknown': _lose_sender,
    }
    mutations.get(case, lambda *args: None)(home, job, case)
    _recover_in_fresh_reader(home)
    if case == 'live':
        assert executions.get_execution(eid)['status'] == 'completed'
    elif case == 'unknown':
        assert jobs.get_job(job['id'])['state'] == 'paused'
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler.run_job(job, execution_id=eid)
        assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
    elif case == 'send-unknown':
        sent = []
        assert delivery_queue.drain(lambda *args: sent.append(args)) == 0
        assert sent == []
        assert delivery_queue.get_status(eid)['status'] == 'unknown'
        assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    else:
        assert delivery_queue.get_status(eid) is None
        saved = jobs.get_job(job['id'])
        assert saved is None or saved['repeat']['completed'] == 0
        assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
        if case in ('failed', 'cancelled'):
            assert executions.get_execution(eid)['status'] == case


def test_binding_drift_is_parked_after_first_observation(tmp_path, monkeypatch, caplog):
    import logging

    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = home / 'scripts' / 'report.py'
    script.write_text('print("completed payload")\\n')
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='telegram:original')
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    _crash_after_result(home, job, 'save')
    script.write_text('print("changed input bytes")\\n')

    caplog.set_level(logging.WARNING, logger='cron.scheduler_authority')
    scheduler_authority.reconcile_pending(allow_connect=False)
    journal = scheduler_authority.journal_path(job['id'], eid)
    record = json.loads(journal.read_text())
    assert record['recovery_deferred']['status'] == 'binding_drift'
    assert record['recovery_deferred']['recorded_binding_sha256']
    assert record['recovery_deferred']['observed_binding_sha256']
    assert any('Cron receipt recovery deferred' in r.message for r in caplog.records)
    assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
    assert delivery_queue.get_status(eid) is None

    caplog.clear()
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert not any('Cron receipt recovery deferred' in r.message for r in caplog.records)
    assert json.loads(journal.read_text())['result'] == record['result']
    assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
    assert delivery_queue.get_status(eid) is None

    jobs.update_job(job['id'], {'deliver': 'telegram:new-target'})
    caplog.clear()
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert any('Cron receipt recovery deferred' in r.message for r in caplog.records)
    assert delivery_queue.get_status(eid) is None

    jobs.update_job(job['id'], {'deliver': 'telegram:original'})
    script.write_text('print("completed payload")\\n')
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert list(jobs._job_output_dir(job['id']).glob('*.md'))
    queued = delivery_queue.get_status(eid)
    assert queued and queued['status'] == 'pending'


def test_unsealed_bot_result_is_refused_without_replay(tmp_path, monkeypatch, caplog):
    import logging

    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = home / 'scripts' / 'report.py'
    original = 'print("completed payload")\\n'
    script.write_text(original)
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='bot-chat')
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    _crash_after_result(home, job, 'save')
    journal = scheduler_authority.journal_path(job['id'], eid)
    record = json.loads(journal.read_text())
    record['job'].pop('_sealed_bot_result_v1', None)
    from cron.scheduler_results import _save
    _save(journal, record)
    script.write_text('print("changed input bytes")\\n')

    caplog.set_level(logging.WARNING, logger='cron.scheduler_authority')
    scheduler_authority.reconcile_pending(allow_connect=False)
    refused = json.loads(journal.read_text())
    assert refused['recovery_refused']['status'] == 'missing_sealed_provenance'
    assert any('without sealed Bot provenance' in r.message for r in caplog.records)
    assert delivery_queue.get_status(eid) is None

    script.write_text(original)
    caplog.clear()
    scheduler_authority.reconcile_pending(allow_connect=False)
    assert not caplog.records
    assert json.loads(journal.read_text())['recovery_refused'] == refused['recovery_refused']
    assert delivery_queue.get_status(eid) is None


@pytest.mark.parametrize('phase', ['save', 'mark', 'handoff'])
def test_completed_result_tail_error_defers_instead_of_overwriting_result(tmp_path, monkeypatch, phase):
    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'scripts' / 'report.py').write_text('print("business result")\n')
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='telegram:original')
    real_save = scheduler.save_job_output
    real_mark = scheduler.mark_job_run
    sends = []
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda job: False)
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *args, **kw: sends.append(args))
    if phase == 'save':
        def failed_save(*args):
            raise OSError('injected output store unavailable')
        monkeypatch.setattr(scheduler, 'save_job_output', failed_save)
    elif phase == 'mark':
        def failed_mark(*args, **kwargs):
            real_mark(*args, **kwargs)
            raise OSError('injected return lost after mark committed')
        monkeypatch.setattr(scheduler, 'mark_job_run', failed_mark)
    else:
        def lost_worker(job):
            assert executions.mark_execution_handoff_pending(job['execution_id'])
            process = _crash_after_result(home, job, 'save')
            return scheduler._wait_for_external_cron_worker(process, execution_id=job['execution_id'])
        monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lost_worker)
    assert scheduler.run_one_job(job) is False
    monkeypatch.setattr(scheduler, 'save_job_output', real_save)
    monkeypatch.setattr(scheduler, 'mark_job_run', real_mark)
    # This process explicitly gave up its row. A foreign reader still sees its live PID
    # and must stand down; its own ticker may finish the deferred tail.
    scheduler_authority.reconcile_pending(allow_connect=False)
    _recover_in_fresh_reader(home)
    eid = job['execution_id']
    assert executions.get_execution(eid)['status'] == 'completed'
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert sends == [], 'tail failure must not replace the completed result with a failure alert'
    queued = delivery_queue.get_status(eid)
    assert queued and queued['content'] == 'business result'


def test_multiple_recovery_readers_consume_one_tail(tmp_path, monkeypatch):
    import concurrent.futures
    import socket
    import threading

    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'scripts' / 'report.py').write_text('print("one result")\n')
    job = jobs.create_job(prompt=None, script='report.py', no_agent=True,
                          schedule='every 1h', deliver='telegram:original')
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    _crash_after_result(home, job, 'save')
    saves = home / 'saved-output-calls'
    # Release two readers at entry. Hold the first output write until the other has read
    # its journal, or met the real per-job lock; the observer never supplies a fake result.
    with socket.socket() as barrier:
        barrier.bind(('127.0.0.1', 0))
        barrier.listen(4)
        address = json.dumps(barrier.getsockname())
        readers = []
        code = '''
            import json, socket, sys
            from pathlib import Path
            from cron import scheduler, scheduler_authority
            from cron import jobs
            request = json.loads(sys.stdin.read())
            connection = socket.create_connection(tuple(request['address']), timeout=15)
            real_read = Path.read_text
            def read(path, *args, **kwargs):
                text = real_read(path, *args, **kwargs)
                if path.parent.name == 'admissions' and path.suffix == '.json':
                    connection.sendall(b'R')
                return text
            Path.read_text = read
            real_lock = jobs._acquire_flock
            def lock(fd, timeout):
                if Path(fd.name).name.startswith('.fire-'):
                    connection.sendall(b'L')
                return real_lock(fd, timeout)
            jobs._acquire_flock = lock
            real_save = scheduler.save_job_output
            def save(*args, **kwargs):
                connection.sendall(b'S')
                assert connection.recv(1) == b'G'
                with Path(request['saves']).open('a') as out: out.write('saved\\n')
                return real_save(*args, **kwargs)
            scheduler.save_job_output = save
            scheduler_authority.reconcile_pending(allow_connect=False)
            connection.close()
            '''
        for _ in range(2):
            child = subprocess.Popen([sys.executable, '-c', textwrap.dedent(code)],
                                     env=dict(os.environ, HERMES_HOME=str(home)),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            child.stdin.write(json.dumps({'address': json.loads(address), 'saves': str(saves)}))
            child.stdin.close()
            child.stdin = None
            readers.append(child)
        connections = [barrier.accept()[0] for _ in readers]
        ready = threading.Barrier(2)

        def coordinate(connection):
            connection.settimeout(15)
            first = connection.recv(1)
            assert first in (b'R', b'L')
            ready.wait(timeout=15)
            while event := connection.recv(1):
                if event == b'S':
                    connection.sendall(b'G')

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(coordinate, connection) for connection in connections]
                for child in readers:
                    stdout, stderr = child.communicate(timeout=30)
                    assert child.returncode == 0, (stdout, stderr)
                for future in futures:
                    future.result(timeout=5)
        finally:
            for connection in connections:
                connection.close()
            for child in readers:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
    assert saves.read_text().splitlines() == ['saved'], 'competing readers repeated the result tail'
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert executions.get_execution(eid)['status'] == 'completed'
    assert delivery_queue.get_status(eid)['status'] == 'pending'


def test_explicit_script_fire_requires_an_owned_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    touched = tmp_path / 'effect'
    (tmp_path / 'scripts' / 'effect.py').write_text(
        f'from pathlib import Path\nPath({str(touched)!r}).write_text("effect")\nprint("ok")\n')
    job = jobs.create_job(prompt=None, script='effect.py', no_agent=True,
                          schedule='every 1h', deliver='local')
    with pytest.raises(scheduler_authority.CronExecutionUnknown):
        scheduler.run_job(job, execution_id='missing-execution')
    assert not touched.exists()
