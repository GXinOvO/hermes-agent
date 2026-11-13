"""Native Cron result -> Bot consumption binding, reusing the producer's delivery row.

The reference is an identifier, not a caller capability. The authenticated native door
reads only canonical profile stores and checks the exact frozen result, input, target,
message and owner before admission. Same-uid file writers remain in the runtime trust
boundary; this is neither a filesystem sandbox nor a new authorization grant.
"""
from copy import deepcopy
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger('cron.scheduler')

_FIELD = '_sealed_bot_result_v1'
_EVENT_SEAL = object()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve()


def freeze_completed_result(job, execution_id, record):
    """Called only by the native result writer AFTER its exact tuple was saved."""
    stage = ['seal']
    try:
        return _freeze_completed_result(job, execution_id, record, stage)
    except Exception as exc:
        with suppress(Exception):
            logger.error('Cron result receipt stage=%s job=%s execution=%s error_type=%s',
                         stage[0], job.get('id'), execution_id, type(exc).__name__)
        raise


def _freeze_completed_result(job, execution_id, record, stage):
    if not any(t.get('platform') == 'bot-chat' for targets in (record.get('binding') or {}).get('targets', {}).values()
               for t in targets):
        return
    from cron.scheduler_results import validate_binding
    from cron.executions import _PROCESS_ID, get_execution
    from cron.scheduler_authority import journal_path
    import os
    row = get_execution(execution_id)
    if (row is None or row['job_id'] != job['id'] or row['status'] != 'running'
            or row['process_id'] != _PROCESS_ID or row['pid'] != os.getpid()):
        raise ValueError('sealed Cron execution conflict')
    saved = json.loads(journal_path(job['id'], execution_id).read_text(encoding='utf-8-sig'))
    if saved != record:
        raise ValueError('sealed Cron producer result not durably saved')
    validate_binding(record, job)
    result = record.get('result')
    if not isinstance(result, list) or len(result) != 4:
        raise ValueError('sealed Cron result missing')
    params = record.get('params') or {}
    if params.get('job_id') != job['id'] or params.get('request_id') != execution_id:
        raise ValueError('sealed Cron producer identity conflict')
    value = {'version':1, 'source_home':str(_home()), 'job_id':job['id'],
             'execution_id':execution_id, 'binding':deepcopy(record['binding']),
             'result_sha256':_hash(result), 'result':deepcopy(result), 'job_sha256':_definition(job),
             'execution': {key: row[key] for key in
                           ('id', 'job_id', 'source', 'process_id', 'pid', 'process_started_at',
                            'status', 'claimed_at', 'started_at')}}
    value['seal'] = _hash(value)
    job[_FIELD] = value
    # The journal also supplies the frozen job to a fresh scheduler when the firer
    # dies before enqueue. Keep the original tuple and binding unchanged.
    record['job'][_FIELD] = deepcopy(value)
    from cron.scheduler_results import _save
    path = journal_path(job['id'], execution_id)
    stage[0] = 'seal_writeback'
    _save(path, record)


def _definition(job):
    from cron.job_definition import JOB_DEFINITION_FIELDS
    return _hash({key:job.get(key) for key in sorted(JOB_DEFINITION_FIELDS | {'id','created_at','interpreter'})})


def _valid_frozen(job, home, execution_id):
    from cron.scheduler_results import validate_binding
    value = deepcopy(job.get(_FIELD))
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('source_home') != str(home) or value.get('execution_id') != execution_id
            or value.get('job_id') != job.get('id') or value.get('job_sha256') != _definition(job)
            or value.get('result_sha256') != _hash(value.get('result')) or value.get('seal') != _hash(
                {k:v for k,v in value.items() if k != 'seal'})):
        raise ValueError('sealed Cron result identity conflict')
    validate_binding({'binding':value.get('binding')}, job)
    return value


def delivery_identity(home, job_id, execution_id, target):
    return hashlib.sha256(json.dumps([str(home), job_id, str(execution_id), str(target)],
                                   ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _row(home, execution_id, key=None):
    """No arbitrary source path, schema creation or queue mutation on a target consumer."""
    root = home / 'cron'
    path = root / 'deliveries.db'
    if root.is_symlink() or path.is_symlink():
        raise ValueError('sealed Cron source store symlink')
    if not path.is_file():
        raise ValueError('sealed Cron native delivery missing')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM deliveries WHERE execution_id=?', (execution_id,)).fetchone()
        if row is None:
            tombstone = conn.execute('SELECT terminal_status FROM delivery_tombstones WHERE execution_id=?',
                                     (execution_id,)).fetchone()
            if tombstone is not None:
                row = {'execution_id': execution_id, 'status': tombstone[0], 'job_json': '{}'}
    if row is None or row['status'] not in {'pending','delivering','delivered','unknown'}:
        raise ValueError('sealed Cron native delivery unknown; do not replay')
    result = dict(row)
    if key is not None and result['job_json'] == '{}':
        record = _deferred_row(root, key, execution_id, strict=result['status'] == 'unknown')
        result.update(job_json=json.dumps(record['job']), content=record['content'],
                      for_failure=record['for_failure'])
    elif result['status'] == 'unknown':
        raise ValueError('sealed Cron native delivery unknown; do not replay')
    return result


def _deferred_row(root, key, execution_id, *, strict):
    pending = root / 'bot_chat_pending' / (key + '.json')
    if pending.parent.is_symlink() or pending.is_symlink():
        raise ValueError('sealed Cron deferred source symlink')
    record = json.loads(pending.read_text(encoding='utf-8-sig'))
    job = record.get('job') or {}
    if (record.get('id') != key or record.get('status') not in {'queued','claimed','transferred'}
            or job.get('execution_id') != execution_id):
        raise ValueError('sealed Cron deferred result unknown')
    if strict and (record['status'] == 'transferred'
            or key != delivery_identity(root.parent, job.get('id'), execution_id, record.get('home'))
            or type(record.get('for_failure')) is not bool or not isinstance(record.get('content'), str)):
        raise ValueError('sealed Cron deferred result unknown')
    return record


def _target_name(home):
    from tools.bot_mode_probe import _profile_name
    return _profile_name(home)


def _check_row(home, row, target, content=None):
    from cron.scheduler_provider import _profile_cron_scope
    from cron.scheduler_delivery import _resolve_delivery_targets
    job = json.loads(row['job_json'])
    with _profile_cron_scope(home):
        value = _valid_frozen(job, home, row['execution_id'])
        from cron.jobs import get_job
        current = get_job(job['id'])
        if current is None or _definition(current) != value['job_sha256']:
            raise ValueError('sealed Cron current input or target drift')
        targets = _resolve_delivery_targets(job, for_failure=bool(row['for_failure']))
    _check_execution(home, row['execution_id'], job, value)
    candidates = {t['chat_id'] for t in targets if t['platform'] == 'bot-chat'}
    if not any((name or _target_name(home)) == target for name in candidates):
        raise ValueError('sealed Cron target drift')
    if job.get('execution_id') != row['execution_id'] or (content is not None and row['content'] != content):
        raise ValueError('sealed Cron content drift')
    # Successful results have a lossless answer lane; failure notices are producer-composed
    # and pinned by the existing queue's exact immutable content + for_failure columns.
    if value['result'][0] is True and not row['for_failure'] and value['result'][2] != row['content']:
        raise ValueError('sealed Cron result drift')
    return job, value


def _check_execution(home, execution_id, job, value):
    path = home / 'cron' / 'executions.db'
    if path.is_symlink() or not path.is_file():
        raise ValueError('sealed Cron execution missing')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        execution = conn.execute('SELECT * FROM executions WHERE id=?', (execution_id,)).fetchone()
    proof = value.get('execution')
    if proof is not None:
        if (not isinstance(proof, dict) or proof.get('id') != execution_id
                or proof.get('job_id') != job['id'] or proof.get('status') != 'running'
                or type(proof.get('pid')) is not int or proof['pid'] <= 0
                or not all(isinstance(proof.get(key), str) and proof[key]
                           for key in ('process_id', 'source', 'claimed_at', 'started_at'))):
            raise ValueError('sealed Cron producer execution conflict')
        if execution is not None and any(execution[key] != proof[key] for key in
                ('job_id', 'source', 'process_id', 'pid', 'process_started_at', 'claimed_at', 'started_at')):
            raise ValueError('sealed Cron native execution conflict')
    if execution is None:
        if proof is None:
            raise ValueError('sealed Cron native execution conflict')
        return  # The native producer sealed the owned running identity before history eviction.
    if execution['job_id'] != job['id'] or execution['status'] not in {'running','completed','failed','unknown'}:
        raise ValueError('sealed Cron native execution conflict')


def canonical_target_home(target):
    from tools.bot_mode_probe import _hermes_root
    source = _home()
    target = Path(target).resolve()
    profile = _target_name(target)
    root = _hermes_root(source)
    expected = root if profile == 'default' else root / 'profiles' / profile
    if expected.is_symlink() or expected.resolve() != target:
        raise ValueError('sealed Cron target home drift')
    return target


def producer_reference(job, content, target_home, owner):
    """The scheduler's native producer reads its own row before crossing the door."""
    if _FIELD not in job:
        if job.get('_cron_result_binding') or _native_execution_exists(job.get('execution_id')):
            raise ValueError('sealed Cron provenance missing; outcome unknown, do not replay')
        return None  # legacy/direct notices never acquire strict by caller assertion
    home = _home()
    target_home = canonical_target_home(target_home)
    execution_id = job.get('execution_id')
    _valid_frozen(job, home, execution_id)
    key = delivery_identity(home, job['id'], execution_id, Path(target_home).resolve())
    row = _row(home, execution_id, key)
    target = _target_name(Path(target_home).resolve())
    saved, value = _check_row(home, row, target, content)
    if saved.get(_FIELD) != job[_FIELD] or saved.get('id') != job.get('id'):
        raise ValueError('sealed Cron input drift')
    ref = {'version':1, 'source_profile':_target_name(home), 'job_id':saved['id'],
           'execution_id':execution_id, 'seal':value['seal'],
           'result_sha256':value['result_sha256'], 'input_sha256':_hash(value['binding']),
           'content_sha256':_hash(row['content']), 'target_profile':target,
           'target_home':str(Path(target_home).resolve()),
           'target_session_id':str(owner.get('session_id') or ''),
           'target_live_session_id':str(owner.get('live_session_id') or owner.get('session_id') or ''),
           'owner_instance_id':str(owner.get('lease_id') or '')}
    if not ref['owner_instance_id']:
        raise ValueError('sealed Cron target owner missing')
    return ref


def _native_execution_exists(execution_id):
    path = _home() / 'cron' / 'executions.db'
    if not execution_id or not path.exists():
        return False
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('sealed Cron source store symlink')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        return conn.execute('SELECT 1 FROM executions WHERE id=?', (execution_id,)).fetchone() is not None


def _source_home(target_home, profile):
    from hermes_constants import PROFILE_ID_RE, named_profile_is_live
    from tools.bot_mode_probe import _hermes_root
    if not isinstance(profile, str) or (profile != 'default' and not PROFILE_ID_RE.fullmatch(profile)):
        raise ValueError('sealed Cron source profile invalid')
    root = _hermes_root(target_home)
    home = root if profile == 'default' else root / 'profiles' / profile
    if home.is_symlink() or (home.parent.name == 'profiles' and home.parent.is_symlink()):
        raise ValueError('sealed Cron source profile symlink')
    if profile != 'default' and not named_profile_is_live(home):
        raise ValueError('sealed Cron source profile missing')
    return home.resolve()


@dataclass(frozen=True)
class _Verified:
    reference: dict
    seal: object


def verify_door(connection, params, ref, entry, *, create_missing=False):
    return _verify_reference(connection, params, ref.session_id, entry.session_id, create_missing=create_missing)


def _verify_reference(connection, params, session_id, live_session_id, *, create_missing=False):
    """The native authenticated ingress, before the first admission or receipt pin."""
    from hermes_state_runtime import RuntimeStoreError
    from cron.scheduler_authority import CronExecutionUnknown
    value = params.get('sealed_cron_result_v1')
    if value is None:
        return None
    try:
        authority = connection.authority
        require_native(connection)
        home = Path(authority.db.db_path).parent.resolve()
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError('sealed Cron reference invalid')
        if not isinstance(session_id, str) or not isinstance(live_session_id, str):
            raise ValueError('sealed Cron target identity invalid')
        source = _source_home(home, value.get('source_profile'))
        row = _row(source, value.get('execution_id'), params['id'])
        job, frozen = _check_row(source, row, _target_name(home))
        from cron.scheduler_delivery import bot_chat_message
        expected = {'version':1, 'source_profile':_target_name(source), 'job_id':job['id'],
                    'execution_id':row['execution_id'], 'seal':frozen['seal'],
                    'result_sha256':frozen['result_sha256'],
                    'input_sha256':_hash(frozen['binding']),
                    'content_sha256':_hash(row['content']), 'target_profile':_target_name(home),
                    'target_home':str(home), 'target_session_id':session_id,
                    'target_live_session_id':live_session_id, 'owner_instance_id':authority.instance_id}
        if (create_missing and not value.get('target_session_id') and not params.get('session_id')
                and value.get('target_live_session_id') == ''):
            expected['target_session_id'] = expected['target_live_session_id'] = ''
        if (value != expected or params.get('message') != bot_chat_message(job, row['content'])
                or params.get('session_id', '') != expected['target_session_id']
                or params.get('author') is not None
                or params.get('notification_category', 'result') != ('diagnostic' if row['for_failure'] else 'result')
                or params.get('id') != delivery_identity(source, job['id'], row['execution_id'], home)):
            raise ValueError('sealed Cron reference conflict')
        return _Verified(deepcopy(expected), _EVENT_SEAL)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, CronExecutionUnknown) as exc:
        raise RuntimeStoreError('admission_conflict') from exc


def require_native(connection):
    from gateway.owner_identity import host_owner_subject
    actor = connection.actor
    if (getattr(connection, 'native_owner', False) is not True
            or 'session:operator' not in actor.capabilities or actor.subject != host_owner_subject()):
        raise ValueError('sealed Cron native producer required')


def _require_pinned_target(authority, actor, session_id):
    from gateway import session_bot as bot
    from hermes_state_runtime import RuntimeStoreError
    row = authority.db.get_session_by_title('Bot Chat')
    if (row is None or authority.db.get_compression_tip(row['id']) != session_id
            or bot.displaceable_bot_chat(authority, row, actor.subject) is not None):
        raise RuntimeStoreError('admission_conflict')


async def prepare(connection, params):
    from gateway import session_bot as bot
    from gateway.session_authorities import require_serving
    from hermes_state_runtime import RuntimeStoreError
    authority, actor = connection.authority, connection.actor
    require_serving(authority)
    authority._require_admission_open()
    create_missing = params.get('create_missing') is True
    require_unadmitted = params.get('require_unadmitted_v1') is True
    if require_unadmitted and create_missing:
        raise RuntimeStoreError('invalid_params')
    delivery_params = {key: value for key, value in params.items()
                       if key not in {'create_missing', 'require_unadmitted_v1'}}
    if create_missing and delivery_params.get('session_id') == '':
        delivery_params.pop('session_id')
    home, key, _, _, _ = bot._delivery_params(authority, actor, delivery_params)
    try:
        require_native(connection)
    except ValueError as exc:
        raise RuntimeStoreError('permission_denied') from exc
    value = params.get('sealed_cron_result_v1')
    if not isinstance(value, dict):
        raise RuntimeStoreError('admission_conflict')
    _verify_reference(connection, params, value.get('target_session_id'), value.get('target_live_session_id'))
    async with bot.mailbox_lock(authority):
        _, record = await bot.read_receipt(home, key)
        if record is not None:
            raise RuntimeStoreError('admission_conflict')
        if not create_missing:
            _require_pinned_target(authority, actor, params.get('session_id'))
            if bot._pin_committed(authority, {'session_id': params['session_id']}, 'bot:' + key):
                raise RuntimeStoreError('admission_conflict')
        ref, _, entry = await bot._target(authority, actor)
        if not create_missing and ref.session_id != params.get('session_id'):
            raise RuntimeStoreError('admission_conflict')
        require_serving(authority)
        result = {'profile_home': str(home), 'session_id': ref.session_id, 'live_session_id': entry.session_id,
                  'lease_id': authority.instance_id, 'authority_epoch': authority.epoch, 'principal_id': actor.subject}
        if require_unadmitted:
            result['unadmitted_v1'] = True
        return result


async def deliver_inside(connection, params):
    from gateway import session_bot as bot
    from hermes_state_runtime import RuntimeStoreError
    authority, actor = connection.authority, connection.actor
    home, key, message, author, category = bot._delivery_params(authority, actor, params)
    identity = (message, author, category)
    authority._require_admission_open()
    async with bot.mailbox_lock(authority):
        root, record = await bot.read_receipt(home, key)
        path = root / f'{key}.json'

        async def answer_existing(record):
            if record is not None and (record.get('sealed_cron_result_v1') is not None
                                       or params.get('sealed_cron_result_v1') is not None):
                try:
                    require_native(connection)
                except ValueError as exc:
                    raise RuntimeStoreError('permission_denied') from exc
                if (record.get('sealed_cron_result_v1') != params.get('sealed_cron_result_v1')
                        or params.get('session_id', '') != record['sealed_cron_result_v1']['target_session_id']):
                    raise RuntimeStoreError('admission_conflict')
            return await bot._receipt_answer(authority, actor, home, record, identity, retry_path=path)

        answer = await answer_existing(record)
        if answer is not None:
            return answer
        try:
            await bot._migrate(authority, actor, home, root)
            _, record = await bot.read_receipt(home, key)
            answer = await answer_existing(record)
            if answer is not None:
                return answer
            if record is not None:
                raise RuntimeStoreError('unknown_execution')
            holder = authority.db.get_session_by_title('Bot Chat')
            if params.get('sealed_cron_result_v1') is not None and params.get('session_id'):
                _require_pinned_target(authority, actor, params['session_id'])
            ref, live, entry = await bot._target(authority, actor)
            pinned = params.get('session_id', entry.session_id)
            if pinned != entry.session_id and not bot._moved_aside_by_this_door(authority, holder, pinned):
                raise RuntimeStoreError('admission_conflict')
            verified = verify_door(connection, params, ref, entry, create_missing=holder is None)
            return await bot._admit(authority, actor, home, root, key, message, ref, live, entry, author=author,
                                    notification_category=category, sealed=verified)
        except RuntimeStoreError as exc:
            raise await bot._held_by_another_writer(home, key, exc) or exc


def verify_receipt(job, receipt):
    frozen = job.get(_FIELD)
    ref = receipt.get('sealed_cron_result_v1')
    if frozen is not None and (not isinstance(ref, dict) or any(
            ref.get(key) != frozen.get(key) for key in ('seal','result_sha256','execution_id','job_id'))):
        raise ValueError('sealed Cron receipt provenance conflict')


def validate_defer(key, job, content, home, for_failure):
    if _FIELD not in job:
        return  # old-format evidence is parked, then quarantined without a replay
    source = _home()
    home = canonical_target_home(home)
    if key != delivery_identity(source, job['id'], job['execution_id'], home):
        raise ValueError('sealed Cron deferred identity conflict')
    row = _row(source, job['execution_id'])
    native, _ = _check_row(source, row, _target_name(home), content)
    if native.get(_FIELD) != job[_FIELD] or bool(row['for_failure']) != for_failure:
        raise ValueError('sealed Cron deferred result conflict')


def bind_event(event, verified):
    if type(verified) is not _Verified or verified.seal is not _EVENT_SEAL:
        raise ValueError('sealed Cron untrusted event binding')
    event._sealed_cron_result = verified


def event_descriptor(event):
    value = getattr(event, '_sealed_cron_result', None)
    return ({'sealed_cron_result_v1':deepcopy(value.reference)}
            if type(value) is _Verified and value.seal is _EVENT_SEAL else {})
