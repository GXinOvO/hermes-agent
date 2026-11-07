import importlib
import re
from types import SimpleNamespace

PLATFORMS = {'mattermost', 'feishu', 'google_chat', 'weixin'}


def module(platform):
    if platform not in PLATFORMS:
        raise ValueError('frozen live HTTP platform unavailable')
    return importlib.import_module('plugins.platforms.' + platform + '.frozen_delivery')


def _saved(job, target):
    from cron import delivery_queue as queue
    from cron.strict_thread_delivery import validate_row
    row = queue.get_status(job.get('execution_id'))
    if row is None or row.get('thread_json') is None:
        return None
    value = validate_row(row)
    if value['binding']['version'] != 2:
        raise RuntimeError('frozen live HTTP saved bundle version changed')
    matches = [(descriptor['plan'], item) for descriptor, item in zip(value['binding']['targets'], value['items'])
               if descriptor['kind'] == 'live-http' and descriptor['route'] == target]
    if len(matches) != 1:
        raise RuntimeError('frozen live HTTP saved target missing or ambiguous')
    return matches[0]


def saved_done(job, target):
    saved = _saved(job, target)
    if saved is None or saved[1]['stage'] != 'done':
        return None
    return {'kind': 'live-http', '_frozen_http_platform': saved[0]['platform'],
            'plan': {'binding': saved[0], 'media': saved[0]['media']}}


async def freeze(state, calls, retained, job, target, saved):
    from hermes_constants import get_hermes_home
    values = dict(vars(state), profile=str(get_hermes_home().resolve()), execution_id=job['execution_id'], target=target)
    helper = module(state.platform_name)
    kwargs = {'saved_binding': saved} if state.platform_name == 'weixin' else {}
    plan = await helper.freeze(state.runtime_adapter, SimpleNamespace(**values), calls, retained, **kwargs)
    if saved is not None and plan['binding'] != saved:
        raise RuntimeError('frozen live HTTP saved plan changed')
    return {'kind': 'live-http', '_frozen_http_platform': state.platform_name, 'plan': plan}


def prepare(state, calls, retained, job, content, failure, loop, *, target):
    from agent.async_utils import safe_schedule_threadsafe
    from cron.strict_delivery import _durable_media
    from cron.strict_thread_delivery import await_fenced
    saved = _saved(job, target)
    if saved is not None and saved[1]['stage'] == 'done':
        frozen = {'kind': 'live-http', '_frozen_http_platform': state.platform_name,
                  'plan': {'binding': saved[0], 'media': saved[0]['media']}}
    else:
        future = safe_schedule_threadsafe(freeze(state, calls, retained, job, target, saved[0] if saved else None), loop)
        if future is None:
            raise RuntimeError('cron frozen live HTTP loop unavailable')
        frozen = await_fenced(future, 60)
    if frozen['plan']['media'] != _durable_media(job, content, failure)[1]:
        raise RuntimeError('cron frozen live HTTP media differs from durable result')
    return frozen


def send(state, frozen):
    import asyncio
    from agent.async_utils import safe_schedule_threadsafe
    from cron.scheduler_delivery import _get_standalone_send_timeout
    from cron.strict_thread_delivery import await_fenced
    timeout = _get_standalone_send_timeout()
    async def submit():
        return await asyncio.wait_for(module(frozen['_frozen_http_platform']).send(frozen['plan']), timeout)
    future = safe_schedule_threadsafe(submit(), state.loop)
    if future is None:
        raise RuntimeError('frozen live HTTP send loop unavailable')
    return await_fenced(future, timeout + 1)


def validate(item, target, binding_hash):
    binding = target['plan']
    if type(binding.get('version')) is int and binding['version'] == 2:
        validator = getattr(module(binding.get('platform')), 'validate_binding', None)
        if validator is None:
            raise ValueError('frozen live HTTP binding version unavailable')
        validator(binding, target['route'])
    else:
        _validate_v1(binding, target['route'])
    return _stage(item, target, binding_hash)


def validate_context(target, parent):
    binding = target['plan']
    if binding.get('version') == 2:
        identity = binding['identity']
        if identity['home'] != parent['home'] or identity['execution'] != parent['job'].get('execution_id'):
            raise ValueError('frozen live HTTP media queue identity changed')


def _validate_v1(binding, route):
    from plugins.platforms.frozen_delivery import digest as byte_digest, encode, validate_url
    required = {'version', 'platform', 'actualdestination', 'parent', 'thread', 'account', 'scope',
                'prepared', 'media', 'transport_sha256'}
    if (set(binding) != required or type(binding['version']) is not int or binding['version'] != 1
            or binding['platform'] not in PLATFORMS or binding['platform'] != route.get('platform')
            or binding['actualdestination'] != str(route.get('chat_id'))
            or not isinstance(binding['parent'], list) or not isinstance(binding['scope'], str)
            or not binding['scope'] or not isinstance(binding['prepared'], list) or not binding['prepared']
            or not isinstance(binding['media'], list)):
        raise ValueError('frozen live HTTP binding invalid')
    if binding['transport_sha256'] != byte_digest(encode({k: v for k, v in binding.items() if k != 'transport_sha256'})):
        raise ValueError('frozen live HTTP transport fingerprint invalid')
    for request in binding['prepared']:
        validate_url(request['url'])
        if (request['method'] != 'POST' or not isinstance(request['headers'], dict)
                or not isinstance(request['body_b64'], str)
                or not isinstance(request['wire_body_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', request['wire_body_sha256'])
                or request['redirects'] is not False or type(request['retries']) is not int or request['retries'] != 0
                or request['auth_replay'] is not False or request['proxy'] is not None):
            raise ValueError('frozen live HTTP request invalid')
    for entry in binding['media']:
        if (not isinstance(entry, list) or len(entry) != 4 or not isinstance(entry[0], str)
                or type(entry[1]) is not bool or not isinstance(entry[2], str)
                or not re.fullmatch('[0-9a-f]{64}', entry[2]) or not isinstance(entry[3], str)):
            raise ValueError('frozen live HTTP media invalid')


def _stage(item, target, binding_hash):
    from cron.strict_thread_delivery import digest
    if not isinstance(item, dict) or set(item) != {'stage', 'receipt', 'receipt_sha256'}:
        raise ValueError('frozen live HTTP stage invalid')
    stage = item['stage']
    if not isinstance(stage, str) or stage not in {'initial', 'target_claimed', 'done'}:
        raise ValueError('frozen live HTTP stage invalid')
    if stage != 'done':
        if item['receipt'] is not None or item['receipt_sha256'] is not None:
            raise ValueError('unacknowledged frozen live HTTP receipt')
    elif (not isinstance(item['receipt'], dict) or item['receipt'].get('ok') is not True
            or item['receipt_sha256'] != digest({'binding': binding_hash, 'target': target, 'ack': item['receipt']})):
        raise ValueError('frozen live HTTP receipt invalid')
    return stage
