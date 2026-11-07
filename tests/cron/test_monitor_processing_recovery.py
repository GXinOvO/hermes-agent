"""Detection deduplication does not acknowledge unfinished monitor analysis."""
import json

import pytest

from cron import executions, jobs, scheduler_authority
from tests.cron import test_completed_result_recovery as recovery


def _produce_terminal_monitor(home, job):
    child = recovery._python(home, '''
        import asyncio, json, os, sys
        import pytest
        from cron import executions, scheduler_authority
        from gateway import session_cron
        from gateway.config import Platform
        from gateway.session_contract import SessionRef
        from gateway.session_local_recovery import local_adapter_map
        from hermes_state_runtime import claim_session_input, settle_session_input
        from tests.cron.test_cron_recovery import _owner
        from tests.cron.test_monitor_kind import _install_agent_stubs
        from pathlib import Path
        job = json.loads(sys.stdin.read())
        assert executions.adopt_claimed_execution(job['execution_id'])
        patch = pytest.MonkeyPatch()
        from hermes_state_registry import acquire
        from tests.cron import test_cron_recovery as fixtures
        patch.setattr(fixtures, 'SessionDB', acquire)
        owner, db = _owner(Path(os.environ['HERMES_HOME']), patch)
        observed = {}
        _install_agent_stubs(patch, observed)
        params = {'job_id': job['id'], 'request_id': job['execution_id'], 'extra_prompt': None}
        async def produce():
            receipt = await session_cron.operation(owner, 'submit', params)
            row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
            policy = local_adapter_map(owner)[Platform.LOCAL].policies[receipt['session_id']]
            await session_cron.execute(owner, SessionRef(owner.profile_id, receipt['session_id']), row, policy)
            result = owner.pending_results[row['admission_id']]
            settle_session_input(db, epoch=owner.epoch, admission_id=row['admission_id'],
                                 generation=row['generation'], outcome='completed', result=result)
            path = scheduler_authority.journal_path(job['id'], job['execution_id'])
            path.parent.mkdir(parents=True, exist_ok=True)
            from utils import atomic_json_write
            atomic_json_write(path, {'params': params, 'receipt': receipt}, mode=0o600, fsync_dir=True)
        asyncio.run(produce())
        assert observed['agent_runs'] == 1
        db.close()
        os._exit(77)
        ''', input=json.dumps(job))
    assert child.returncode == 77, (child.stdout, child.stderr)


def _consume_terminal_monitor(home, *, crash_after_finish=False):
    child = recovery._python(home, '''
        import asyncio, json, os, sys
        from pathlib import Path
        import pytest
        from cron import executions, scheduler_authority
        from gateway import session_cron
        from tests.cron.test_cron_recovery import _owner
        patch = pytest.MonkeyPatch()
        owner, db = _owner(Path(os.environ['HERMES_HOME']), patch)
        async def consume():
            if json.loads(sys.stdin.read()):
                real = executions.recover_receipted_execution
                def finish(*args, **kwargs):
                    real(*args, **kwargs)
                    os._exit(78)
                patch.setattr(executions, 'recover_receipted_execution', finish)
            session_cron.bind_owner(owner)
            try:
                await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
            finally:
                session_cron.unbind_owner(owner)
        asyncio.run(consume())
        db.close()
        ''', input=json.dumps(crash_after_finish))
    assert child.returncode == (78 if crash_after_finish else 0), (child.stdout, child.stderr)


def test_observed_content_survives_owner_loss_without_becoming_processed(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    calls = home / 'source-calls'
    (home / 'scripts' / 'source.py').write_text(
        f'from pathlib import Path\np=Path({str(calls)!r})\n'
        'with p.open("a") as f: f.write("source\\n")\nprint("anomaly")\n')
    job = jobs.create_job(prompt='analyze the anomaly', monitor_script='source.py',
                          schedule='every 1h', deliver='local')
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    child = recovery._python(home, '''
        import json, os, sys
        from cron import executions, monitor
        job = json.loads(sys.stdin.read())
        assert executions.adopt_claimed_execution(job['execution_id'])
        assert monitor.check_monitor(job).changed
        os._exit(76)
        ''', input=json.dumps(job))
    assert child.returncode == 76, (child.stdout, child.stderr)
    state = jobs.get_job(job['id'])['monitor_state']
    observed_hash = state['last_output_hash']
    assert state.get('observation', {}).get('execution_id') == eid, 'observed hash lacks its analysis execution'
    assert state['observation']['analysis_status'] == 'pending'
    assert state['observation']['consumed'] is False
    recovery._recover_in_fresh_reader(home)
    child = recovery._python(home, '''
        import json, sys
        from cron import jobs, scheduler
        job = jobs.get_job(sys.stdin.read())
        early, _, _ = scheduler._apply_monitor_gate(job, job['id'], job['name'], None)
        assert early[0] is True and early[2] == scheduler.SILENT_MARKER
        assert 'analysis unknown' in early[1], early[1]
        print(json.dumps(jobs.get_job(job['id'])['monitor_state']))
        ''', input=job['id'])
    assert child.returncode == 0, (child.stdout, child.stderr)
    state = json.loads(child.stdout)
    assert state['last_output_hash'] == observed_hash
    assert state['observation']['analysis_status'] == 'unknown'
    assert state['observation']['consumed'] is False
    assert 'processed' not in state
    assert calls.read_text().splitlines() == ['source', 'source']
    assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
    assert scheduler_authority.journal_path(job['id'], eid).exists() is False


@pytest.mark.parametrize('crash_after_finish', [False, True])
def test_terminal_monitor_result_consumes_original_observation_without_rereading_source(
    tmp_path, monkeypatch, crash_after_finish,
):
    home = tmp_path / 'profile'
    (home / 'scripts').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    calls = home / 'source-calls'
    (home / 'scripts' / 'source.py').write_text(
        f'from pathlib import Path\np=Path({str(calls)!r})\n'
        'with p.open("a") as f: f.write("source\\n")\nprint("anomaly")\n')
    job = jobs.create_job(prompt='analyze the anomaly', monitor_script='source.py',
                          schedule='every 1h', deliver='local')
    eid = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(eid)
    _produce_terminal_monitor(home, job)
    state = jobs.get_job(job['id'])['monitor_state']
    observed_hash = state['last_output_hash']
    assert state['observation']['consumed'] is False
    _consume_terminal_monitor(home, crash_after_finish=crash_after_finish)
    state = jobs.get_job(job['id'])['monitor_state']
    assert state['observation']['analysis_status'] == 'completed'
    assert state['observation']['consumed'] is True
    assert state['processed']['execution_id'] == eid
    assert state['processed']['output_hash'] == observed_hash == state['last_output_hash']
    assert executions.get_execution(eid)['status'] == 'completed'
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    _consume_terminal_monitor(home)
    assert not scheduler_authority.journal_path(job['id'], eid).exists()
    assert calls.read_text().splitlines() == ['source']
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
