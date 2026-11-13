from copy import deepcopy
import json

import pytest

from cron.strict_thread_delivery import bind_row, digest, validate


def _plans():
    identity = {
        'home': '/profile', 'database': [1, 2],
        'endpoint': {'profile_id': '/profile', 'instance_id': 'old', 'authority_epoch': 1,
                     'runtime_protocol': 1, 'api_origin': 'http://127.0.0.1:1001'},
        'params': {'id': 'a' * 64, 'profile': 'default', 'session_id': 'chat', 'message': 'body',
                   'notification_category': 'result',
                   'sealed_cron_result_v1': {'seal': 'seal', 'target_session_id': 'chat',
                                             'target_live_session_id': 'chat', 'owner_instance_id': 'old'}},
    }
    route = {'platform': 'bot-chat', 'chat_id': '', 'thread_id': None}
    binding = {'version': 2, 'home': '/profile', 'job': {'id': 'job'}, 'content_sha256': 'b' * 64,
               'for_failure': False, 'targets': [
                   {'kind': 'slack', 'route': {'platform': 'slack', 'chat_id': 'one'},
                    'plan': {'sha256': 'c' * 64}},
                   {'kind': 'bot-chat', 'route': route,
                    'plan': {'identity': identity, 'sha256': digest(identity)}},
                   {'kind': 'slack', 'route': {'platform': 'slack', 'chat_id': 'two'},
                    'plan': {'sha256': 'd' * 64}},
               ]}
    saved = {'binding': binding, 'binding_sha256': digest(binding), 'items': [
        {'stage': 'done', 'receipt': {'ok': True, 'message_id': '1'}, 'receipt_sha256': None},
        {'stage': 'initial', 'receipt': None, 'receipt_sha256': None},
        {'stage': 'initial', 'receipt': None, 'receipt_sha256': None},
    ]}
    saved['items'][0]['receipt_sha256'] = digest({'binding': saved['binding_sha256'],
        'target': binding['targets'][0], 'ack': saved['items'][0]['receipt']})
    validate(saved)
    prepared = deepcopy(saved)
    prepared['items'] = [{'stage': 'initial', 'receipt': None, 'receipt_sha256': None} for _ in prepared['items']]
    identity = prepared['binding']['targets'][1]['plan']['identity']
    identity['endpoint'].update(instance_id='new', authority_epoch=2, api_origin='http://127.0.0.1:1002')
    identity['params']['sealed_cron_result_v1']['owner_instance_id'] = 'new'
    _sign(prepared)
    return saved, prepared


def _sign(plan):
    for target in plan['binding']['targets']:
        if target['kind'] == 'bot-chat':
            target['plan']['sha256'] = digest(target['plan']['identity'])
    plan['binding_sha256'] = digest(plan['binding'])
    for item, target in zip(plan['items'], plan['binding']['targets']):
        if item['receipt'] is not None:
            item['receipt_sha256'] = digest({'binding': plan['binding_sha256'], 'target': target,
                                             'ack': item['receipt']})


def test_rebind_preserves_durable_prefix():
    saved, prepared = _plans()
    actual = bind_row({'thread_json': json.dumps(saved)}, prepared)
    assert actual['items'][0]['stage'] == 'done'
    assert actual['items'][0]['receipt'] == saved['items'][0]['receipt']
    assert actual['binding']['targets'][1]['plan']['identity']['endpoint']['instance_id'] == 'new'
    assert validate(actual) == actual
    assert saved['binding']['targets'][1]['plan']['identity']['endpoint']['instance_id'] == 'old'


@pytest.mark.parametrize('drift', ['profile', 'protocol', 'extra_identity', 'extra_endpoint', 'database',
                                    'message', 'session', 'seal', 'owner_mismatch', 'other_target',
                                    'completed_target', 'claimed_target'])
def test_rebind_rejects_other_identity_or_stage_drift(drift):
    saved, prepared = _plans()
    identity = prepared['binding']['targets'][1]['plan']['identity']
    if drift == 'profile':
        identity['endpoint']['profile_id'] = '/foreign'
    elif drift == 'protocol':
        identity['endpoint']['runtime_protocol'] = 2
    elif drift == 'extra_identity':
        identity['unexpected'] = 'changed'
    elif drift == 'extra_endpoint':
        identity['endpoint']['unexpected'] = 'changed'
    elif drift == 'database':
        identity['database'] = [1, 3]
    elif drift == 'message':
        identity['params']['message'] = 'different'
    elif drift == 'session':
        identity['params']['session_id'] = 'other'
    elif drift == 'seal':
        identity['params']['sealed_cron_result_v1']['seal'] = 'different'
    elif drift == 'owner_mismatch':
        identity['params']['sealed_cron_result_v1']['owner_instance_id'] = 'different'
    elif drift == 'other_target':
        prepared['binding']['targets'][0]['route']['chat_id'] = 'other'
    elif drift == 'completed_target':
        saved['items'][1] = {'stage': 'done', 'receipt': {'ok': True}, 'receipt_sha256': None}
    elif drift == 'claimed_target':
        saved['items'][1] = {'stage': 'target_claimed', 'receipt': None, 'receipt_sha256': None}
    _sign(saved)
    _sign(prepared)
    validate(saved)
    validate(prepared)
    with pytest.raises(ValueError, match='strict thread saved plan drift'):
        bind_row({'thread_json': json.dumps(saved)}, prepared)
