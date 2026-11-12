import json
from pathlib import Path


def _saved(job, target):
    from cron import delivery_queue as queue
    from cron.strict_thread_delivery import validate_row
    row = queue.get_status(job.get('execution_id'))
    if row is None or row.get('thread_json') is None:
        return None
    value = validate_row(row)
    if value['binding']['version'] != 2:
        return None
    matches = [(descriptor, item) for descriptor, item in zip(value['binding']['targets'], value['items'])
               if descriptor['kind'] == 'bot-chat' and descriptor['route'] == target]
    if len(matches) != 1:
        raise RuntimeError('strict Bot saved target missing or ambiguous')
    return matches[0]


def _endpoint(endpoint):
    return {key: getattr(endpoint, key) for key in
            ('profile_id', 'instance_id', 'authority_epoch', 'runtime_protocol', 'api_origin')}


def waiting(row, plan):
    if not isinstance(plan, dict) or 'targets' not in plan:
        return False
    from cron.strict_thread_delivery import validate_row
    if row['thread_json'] is not None:
        saved = validate_row(row)
        for index, item in enumerate(saved['items']):
            if item['stage'] != 'done':
                frozen = plan['targets'][index][3]
                return (isinstance(frozen, dict) and frozen.get('kind') == 'bot-chat'
                        and (frozen.get('receipt') or {}).get('status') in {'queued', 'claimed'})
        return False
    if plan.get('thread') is not None or len(plan['targets']) != 1:
        return False
    target = plan['targets'][0][0]
    if target['platform'] != 'bot-chat':
        return False
    from cron.bot_chat_legacy import pending_status
    from cron.scheduler_delivery import bot_chat_message
    from cron.sealed_bot_result import canonical_target_home, delivery_identity, verify_receipt
    from gateway.session_bot import _canonical_record_error, _require_same_delivery
    from gateway.owner_identity import host_owner_subject
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home
    from tools.bot_live_delivery import read_delivery_result
    from types import SimpleNamespace
    job, content, failure = json.loads(row['job_json']), row['content'], bool(row['for_failure'])
    home = canonical_target_home(get_profile_dir(target['chat_id']) if target['chat_id'] else get_hermes_home())
    key = delivery_identity(get_hermes_home().resolve(), job['id'], job['execution_id'], home)
    receipt = read_delivery_result(home, key)
    if receipt is None:
        return pending_status(key, job, content, home, for_failure=failure) in {'queued', 'claimed'}
    if receipt.get('status') not in {'queued', 'claimed'}:
        return False
    verify_receipt(job, receipt)
    if _canonical_record_error(Path(key + '.json'), receipt) or receipt.get('profile_home') != str(home):
        raise ValueError('strict Bot pending receipt identity mismatch')
    _require_same_delivery(receipt, SimpleNamespace(subject=host_owner_subject()), bot_chat_message(job, content),
                           None, 'diagnostic' if failure else 'result')
    return True


def suppressed(target, job, content):
    from cron.bot_chat_legacy import pending_status
    from cron.sealed_bot_result import canonical_target_home, delivery_identity
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home
    from tools.bot_live_delivery import read_delivery_result
    home = canonical_target_home(get_profile_dir(target['chat_id']) if target['chat_id'] else get_hermes_home())
    key = delivery_identity(get_hermes_home().resolve(), job['id'], job['execution_id'], home)
    return (not warning_notifications_enabled('tui', load_user_config_effective(home / 'config.yaml'))
            and read_delivery_result(home, key) is None
            and pending_status(key, job, content, home, for_failure=True) is None)


def freeze(target, job, content, failure):
    from cron.scheduler_delivery import bot_chat_message
    from cron.sealed_bot_result import canonical_target_home, delivery_identity, producer_reference
    from hermes_cli.gateway_client import _session_ticket, gateway_ws_target
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home
    from tools.bot_live_delivery import find_canonical_live_owner, read_delivery_result
    home = canonical_target_home(get_profile_dir(target['chat_id']) if target['chat_id'] else get_hermes_home())
    saved = _saved(job, target)
    item = None
    receipt = None
    prepared = None
    if saved is None:
        discovery = discover_gateway_endpoint(home, timeout=5)
        if discovery.state != 'ready' or discovery.endpoint is None:
            raise RuntimeError('strict Bot authority unavailable')
        owner = find_canonical_live_owner(home)
        if owner is None or owner['lease_id'] != discovery.endpoint.instance_id:
            raise RuntimeError('strict Bot owner changed during preflight')
        ref = producer_reference(job, content, home, owner)
        if ref is None:
            raise RuntimeError('strict Bot result provenance unavailable')
        params = {'id': delivery_identity(get_hermes_home().resolve(), job['id'], job['execution_id'], home),
                  'profile': ref['target_profile'],
                  'session_id': owner['session_id'], 'message': bot_chat_message(job, content),
                  'notification_category': 'diagnostic' if failure else 'result', 'sealed_cron_result_v1': ref}
        stat = (home / 'state.db').stat()
        identity = {'home': str(home), 'database': [stat.st_dev, stat.st_ino],
                    'endpoint': _endpoint(discovery.endpoint), 'params': params}
    else:
        descriptor, item = saved
        identity = json.loads(json.dumps(descriptor['plan']['identity']))
        params = identity['params']
        if identity['home'] != str(home):
            raise RuntimeError('strict Bot home changed')
        owner = {'session_id': params['session_id'],
                 'live_session_id': params['sealed_cron_result_v1']['target_live_session_id'],
                 'lease_id': params['sealed_cron_result_v1']['owner_instance_id']}
        if (producer_reference(job, content, home, owner) != params['sealed_cron_result_v1']
                or params['message'] != bot_chat_message(job, content)
                or params['notification_category'] != ('diagnostic' if failure else 'result')):
            raise RuntimeError('strict Bot saved payload changed')
        if item['stage'] == 'done':
            return {'kind': 'bot-chat', 'identity': identity}, identity
    receipt = read_delivery_result(home, params['id'])
    if item is not None and item['stage'] == 'bot_waiting':
        if receipt is None:
            return {'kind': 'bot-chat', 'identity': identity, 'payload': json.dumps(params),
                    'prepared': None, 'receipt': None}, identity
        if receipt.get('admission_id') != item['receipt']['admission_id']:
            raise RuntimeError('strict Bot saved admission changed')
    if receipt is not None:
        _validate_receipt(identity, receipt, cached=True)
    else:
        discovery = discover_gateway_endpoint(home, timeout=5)
        if (discovery.state != 'ready' or discovery.endpoint is None
                or (saved is None and _endpoint(discovery.endpoint) != identity['endpoint'])):
            raise RuntimeError('strict Bot authority changed after plan was saved')
        ticket = _session_ticket(home, discovery.endpoint)
        url, protocols = gateway_ws_target(discovery.endpoint, ticket)
        prepared = (url, tuple(protocols))
        identity['endpoint'] = _endpoint(discovery.endpoint)
        identity = _prepare_target(identity, home, job, content, prepared, saved is not None)
        params = identity['params']
        ticket = _session_ticket(home, discovery.endpoint)
        url, protocols = gateway_ws_target(discovery.endpoint, ticket)
        prepared = (url, tuple(protocols))
    return {'kind': 'bot-chat', 'identity': identity, 'payload': json.dumps(params),
            'prepared': prepared, 'receipt': receipt}, identity


def _prepare_target(identity, home, job, content, prepared, saved):
    from cron.sealed_bot_result import producer_reference
    from gateway.owner_identity import host_owner_subject
    from tools.bot_live_delivery import _authority_rpc
    params = identity['params']
    rpc_params = dict(params)
    if saved:
        reference = dict(params['sealed_cron_result_v1'])
        reference['owner_instance_id'] = identity['endpoint']['instance_id']
        rpc_params['sealed_cron_result_v1'] = reference
        rpc_params['require_unadmitted_v1'] = True
    owner = _authority_rpc(home, 'bot_relay.prepare', {**rpc_params, 'create_missing': not saved},
                           prepared=prepared)
    if (not isinstance(owner, dict) or owner.get('profile_home') != str(home)
            or (saved and owner.get('unadmitted_v1') is not True)
            or owner.get('lease_id') != identity['endpoint']['instance_id']
            or owner.get('authority_epoch') != identity['endpoint']['authority_epoch']
            or owner.get('principal_id') != host_owner_subject()
            or any(not isinstance(owner.get(key), str) or not owner[key] for key in ('session_id', 'live_session_id'))):
        raise RuntimeError('strict Bot prepared target unavailable or mismatched')
    reference = producer_reference(job, content, home, owner)
    if saved:
        previous = dict(params['sealed_cron_result_v1'])
        current = dict(reference)
        previous.pop('owner_instance_id', None)
        current.pop('owner_instance_id', None)
        if params['session_id'] != owner['session_id'] or previous != current:
            raise RuntimeError('strict Bot prepared target changed after plan was saved')
    params.update(session_id=owner['session_id'], sealed_cron_result_v1=reference)
    return identity


def _validate_receipt(identity, receipt, *, cached):
    from gateway.owner_identity import host_owner_subject
    from gateway.session_bot import _canonical_record_error, _require_same_delivery
    from types import SimpleNamespace
    params = identity['params']
    if not isinstance(receipt, dict):
        raise RuntimeError('strict Bot admission ACK unavailable or mismatched')
    refused = receipt.get('refused') is True and receipt.get('status') == 'failed' and 'admission_id' not in receipt
    if cached:
        required = ('profile_home', 'session_id', 'principal_id') if refused else ('admission_id', 'profile_home', 'session_id', 'principal_id')
        problem = _canonical_record_error(Path(params['id'] + '.json'), receipt, required)
        if problem is not None:
            raise RuntimeError('strict Bot malformed receipt: ' + problem)
        _require_same_delivery(receipt, SimpleNamespace(subject=host_owner_subject()),
                               params['message'], params.get('author'), params['notification_category'])
    if (receipt.get('delivery_id') != params['id']
            or receipt.get('profile_home') != identity['home']
            or receipt.get('session_id') != params['session_id']
            or receipt.get('message') != params['message']
            or receipt.get('sealed_cron_result_v1') != params['sealed_cron_result_v1']
            or (not refused and (not isinstance(receipt.get('admission_id'), str) or not receipt['admission_id']))
            or (receipt.get('refused') is True and not (cached and refused))
            or receipt.get('status') not in {'queued', 'claimed', 'ambiguous', 'settled', 'failed', 'cancelled'}):
        raise RuntimeError('strict Bot admission ACK unavailable or mismatched')
    if refused:
        return receipt
    from gateway.session_bot_admission_receipt import validate
    validate(identity['home'], params, receipt)
    return receipt


def recheck_refusal(frozen):
    from tools.bot_live_delivery import read_delivery_result
    identity = frozen['identity']
    receipt = read_delivery_result(Path(identity['home']), identity['params']['id'])
    _validate_receipt(identity, receipt, cached=True)
    if receipt.get('refused') is not True:
        raise RuntimeError('strict Bot refusal changed after claim')


def send(frozen):
    from tools.bot_live_delivery import _authority_rpc, read_delivery_result
    identity = frozen['identity']
    home = Path(identity['home'])
    stat = (home / 'state.db').stat()
    if [stat.st_dev, stat.st_ino] != identity['database']:
        raise RuntimeError('strict Bot database changed after claim')
    params = json.loads(frozen['payload'])
    receipt = frozen['receipt']
    if receipt is None:
        current = read_delivery_result(home, params['id'])
        if current is None:
            receipt = _authority_rpc(home, 'bot_relay.deliver', params, prepared=frozen['prepared'])
            receipt = _validate_receipt(identity, receipt, cached=False)
        else:
            receipt = _validate_receipt(identity, current, cached=True)
    else:
        current = read_delivery_result(home, params['id'])
        current = _validate_receipt(identity, current, cached=True)
        if current['admission_id'] != receipt['admission_id']:
            raise RuntimeError('strict Bot receipt admission changed after claim')
        receipt = current
    status = receipt['status']
    if status in {'queued', 'claimed'}:
        from cron.strict_delivery import DeferredDelivery
        evidence = {'ok': True, 'delivery_id': params['id'], 'session_id': receipt['session_id'],
                    'admission_id': receipt['admission_id']}
        raise DeferredDelivery(f"strict Bot consumer {status}; completion unverified; do not resend",
                               receipt=evidence)
    if status != 'settled':
        raise RuntimeError(f"strict Bot consumer {status}: {receipt.get('error') or receipt.get('reason') or 'not completed'}")
    return {'ok': True, 'delivery_id': params['id'], 'session_id': receipt['session_id'],
            'admission_id': receipt['admission_id'], 'consumer_status': status}
