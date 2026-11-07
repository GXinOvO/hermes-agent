"""Persist completed cron results in the existing admissions journal.

Script-only execution has no session admission. Its prepared record is uncertainty,
never permission to repeat the script; only the completed tuple authorizes its tail.
"""
import json
import hashlib
import os
from contextlib import contextmanager


def _save(path, record):
    from utils import atomic_json_write
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record, mode=0o600, fsync_dir=True)


def _home():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home().resolve())


def result_binding(job):
    """Bind script bytes and the resolved send addresses, including home-channel indirection."""
    from cron import scheduler
    from cron.scheduler_script import _resolve_script_path
    inputs = {}
    for field in ('script', 'monitor_script'):
        if job.get(field):
            path, error = _resolve_script_path(job[field])
            inputs[field] = ({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                             if path else {'error': error})
    targets = {lane: scheduler._resolve_delivery_targets(job, for_failure=failure)
               for lane, failure in (('success', False), ('failure', True))}
    return {'home': _home(), 'inputs': inputs, 'targets': targets}


def validate_binding(record, job):
    from cron.scheduler_authority import CronExecutionUnknown
    binding = record.get('binding')
    if binding is not None and binding != result_binding(job):
        raise CronExecutionUnknown('Cron result input or resolved delivery target changed')


def run_script_job(job, *, execution_id, cancel_event=None):
    """Keep the low-level tuple API; explicit production attempts acquire a durable receipt."""
    from cron import scheduler
    from cron.executions import _PROCESS_ID, get_execution
    from cron.scheduler_authority import CronExecutionUnknown, journal_path

    def run():
        name = str(job.get('name') or job.get('prompt') or job['id'] or 'cron job')
        return scheduler._run_no_agent_job(job, job['id'], name, cancel_event)

    # Legacy run_job callers only execute; they own saving, delivery and accounting. Do not
    # turn a call without an execution identity into a scheduler fire as a side effect.
    if not execution_id:
        return run()
    row = get_execution(execution_id)
    if (row is None or row['job_id'] != job['id'] or row['status'] != 'running'
            or row['process_id'] != _PROCESS_ID or row['pid'] != os.getpid()):
        raise CronExecutionUnknown('Cron script execution is not an owned running attempt')
    path = journal_path(job['id'], execution_id)
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        validate_record(record, job['id'], execution_id)
        if not recovery_job_matches(record, job):
            raise CronExecutionUnknown('Cron script input changed for the same execution')
        validate_binding(record, job)
        if 'result' in record:
            return tuple(record['result'])
        raise CronExecutionUnknown('Cron script outcome unknown; no automatic re-execution')

    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=scheduler._get_hermes_home())
    record = {
        'params': {'job_id': job['id'], 'request_id': execution_id, 'extra_prompt': None},
        'receipt': None, 'local_script': True, 'home': _home(), 'job': dict(job),
        'binding': result_binding(job),
    }
    _save(path, record)
    result = run()
    record['result'] = list(result)
    record['job'] = dict(job)
    try:
        _save(path, record)
        from cron.sealed_bot_result import freeze_completed_result
        freeze_completed_result(job, execution_id, record)
    except Exception as exc:
        raise CronExecutionUnknown('Cron script finished but its result receipt could not be saved') from exc
    return result


def validate_record(record, job_id, execution_id):
    """A result is meaningful only under the home and exact attempt that produced it."""
    from cron.scheduler_authority import CronExecutionUnknown
    params = record['params']
    if ((params['job_id'], params['request_id']) != (job_id, execution_id)
            or record.get('home', _home()) != _home()):
        raise CronExecutionUnknown('Cron completed result identity conflict')


@contextmanager
def recovery_record(journal):
    """Serialize consumers on the existing per-job fire lock and reread inside it."""
    from cron.jobs import _fire_job_lock
    from cron.scheduler_authority import CronExecutionUnknown, journal_path

    try:
        prepared = json.loads(journal.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        yield None
        return
    params = prepared['params']
    if journal != journal_path(params['job_id'], params['request_id']):
        raise CronExecutionUnknown('Cron journal identity conflict')
    with _fire_job_lock(params['job_id']) as acquired:
        if not acquired:
            yield None
            return
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            yield None
            return
        if record['params'] != params:
            raise CronExecutionUnknown('Cron journal identity changed while waiting for recovery')
        yield record


def record_completed_result(job, execution_id, result):
    """Durably checkpoint the real return value before the first save/delivery/mark."""
    from cron.scheduler_authority import CronExecutionUnknown, journal_path
    path = journal_path(job['id'], execution_id)
    record = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
        'params': {'job_id': job['id'], 'request_id': execution_id, 'extra_prompt': None},
        'receipt': None,
    }
    validate_record(record, job['id'], execution_id)
    if 'result' in record and record['result'] != list(result):
        raise CronExecutionUnknown('Cron completed result conflicts with its durable receipt')
    record.update(home=_home(), job=dict(job), result=list(result))
    record.setdefault('binding', result_binding(job))
    job['_cron_result_binding'] = record['binding']
    try:
        _save(path, record)
        from cron.sealed_bot_result import freeze_completed_result
        freeze_completed_result(job, execution_id, record)
    except Exception as exc:
        raise CronExecutionUnknown('Cron completed result receipt could not be saved') from exc


def recovery_job_matches(record, job):
    """Never move an old completion onto a deleted/replaced job or a new source/target."""
    from cron.job_definition import JOB_DEFINITION_FIELDS
    saved = record['job']
    fields = JOB_DEFINITION_FIELDS | {'id', 'created_at', 'interpreter'}
    return job is not None and all(saved.get(key) == job.get(key) for key in fields)


def defer_completed_tail(job, execution_id, error, result_ready, fence):
    """A tail outage cannot replace a durable business result with a new failed run."""
    from cron import scheduler
    from cron.executions import give_up_execution
    if not (result_ready and isinstance(error, Exception)
            and not fence.operator_cancelled() and not fence.lost()):
        return False
    scheduler.logger.warning('Cron %s completed; tail recovery deferred: %s', job['id'], error)
    give_up_execution(execution_id, reason=f'Completed result awaiting tail recovery: {error}')
    return True


def defer_unverified_run(job, execution_id, error, result_ready, fence):
    """Uncertain execution and known-result tail outages never become fresh failed fires."""
    from cron import scheduler
    from cron.executions import give_up_execution
    from cron.scheduler_authority import CronExecutionUnknown, pause_for_unknown
    if isinstance(error, CronExecutionUnknown):
        pause_for_unknown(job['id'], execution_id, str(error))
        give_up_execution(execution_id, reason=str(error))
        scheduler.logger.error('Cron %s paused with unverified admission: %s', job['id'], error)
        return True
    return defer_completed_tail(job, execution_id, error, result_ready, fence)


def local_recovery_state(record, params, journal):
    """Select a sealed result/refusal or park a script whose outcome was never sealed."""
    from cron.jobs import get_job, pause_job
    from cron.scheduler_authority import CronExecutionUnknown, _pause_identity
    validate_record(record, params['job_id'], params['request_id'])
    validate_binding(record, record.get('job') or get_job(params['job_id']) or {})
    if 'result' in record:
        if not recovery_job_matches(record, get_job(params['job_id'])):
            raise CronExecutionUnknown('Cron completed result retained after job source/target changed')
        return {'status': 'terminal', 'result': record['result'], 'job': record['job']}
    if record.get('local_script'):
        if not record.get('pause_recorded'):
            reason = 'Cron script outcome unknown; no automatic re-execution'
            record['pause_recorded'] = _pause_identity(pause_job(params['job_id'], reason=reason))
            _save(journal, record)
        return {'status': 'deferred'}
    if 'refusal' in record:
        return {'status': 'terminal', 'result': record['refusal'], 'job': record['job']}
    return None


def completed_worker_result_pending(job, execution_id):
    """A dead/returned worker's saved result takes precedence over unknown-failure bookkeeping."""
    from cron.executions import FIRER_FINISHED_STATES, get_execution
    from cron.scheduler_authority import journal_path
    path = journal_path(job['id'], execution_id)
    if not path.exists():
        return False
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    validate_record(record, job['id'], execution_id)
    row = get_execution(execution_id)
    return bool('result' in record and row and row['job_id'] == job['id']
                and row['status'] not in FIRER_FINISHED_STATES)
