import importlib
import re
from types import SimpleNamespace


def module(platform):
    if platform not in {'discord', 'telegram', 'matrix'}:
        raise ValueError('strict native platform unavailable')
    return importlib.import_module('plugins.platforms.' + platform + '.strict_thread')


def freeze(state, calls, retained, *, new_thread):
    values = dict(vars(state))
    values['origin'] = dict(state.origin)
    if state.origin.get('platform') != state.platform_name:
        values['origin'].pop('scope_id', None)
    plan = module(state.platform_name).freeze(state.runtime_adapter, SimpleNamespace(**values),
                                              calls, retained, new_thread=new_thread)
    return {'kind': 'native', '_native_platform': state.platform_name, 'plan': plan, 'new_thread': new_thread}


def legacy_target(job, target):
    from cron import delivery_queue as queue
    from cron.strict_thread_delivery import validate_row
    row = queue.get_status(job.get('execution_id'))
    if row is None or row.get('thread_json') is None:
        return False
    value = validate_row(row)
    return value['binding']['version'] == 2 and any(
        item['kind'] == 'telegram' and item['route'] == target for item in value['binding']['targets'])


def handles(job, target):
    platform = target['platform']
    return platform in {'discord', 'telegram', 'matrix'} and not (platform == 'telegram' and legacy_target(job, target))


def prepare(state, calls, retained, job, content, failure):
    from cron.strict_delivery import _durable_media
    new_thread = bool(state.mirror_this_target and not state.in_channel_surface and not state.thread_id)
    if new_thread:
        capability = getattr(module(state.platform_name), 'supports_new_thread', None)
        if capability is None or not capability(state.runtime_adapter, str(state.chat_id)):
            new_thread = False
    frozen = freeze(state, calls, retained, new_thread=new_thread)
    if frozen['plan']['binding']['media'] != _durable_media(job, content, failure)[1]:
        raise RuntimeError('cron frozen native media identity differs from durable result')
    return frozen


def validate_item(item, target, binding_hash):
    from cron.strict_thread_delivery import digest, encode
    platform = target['plan'].get('platform')
    helper = module(platform)
    binding = target['plan']
    required = {'version', 'platform', 'parent', 'scope_id', 'chat_type', 'thread_id', 'root',
                'messages', 'media', 'transport_sha256', 'route_sha256'}
    if (set(binding) != required or type(binding['version']) is not int or binding['version'] != 1
            or not isinstance(binding['parent'], str) or not binding['parent']
            or not isinstance(binding['scope_id'], str) or binding['chat_type'] not in {'dm', 'group', 'thread', 'channel'}
            or binding['thread_id'] is not None and not isinstance(binding['thread_id'], str)
            or not isinstance(binding['messages'], list) or not binding['messages'] or not isinstance(binding['media'], list)):
        raise ValueError('invalid strict native binding')
    for field in ('transport_sha256', 'route_sha256'):
        if not isinstance(binding[field], str) or not re.fullmatch('[0-9a-f]{64}', binding[field]):
            raise ValueError('invalid strict native binding fingerprint')
    if not isinstance(item, dict) or set(item) != {'stage', 'receipt', 'receipt_sha256'}:
        raise ValueError('invalid strict native stage schema')
    stage = item['stage']
    threaded = target['kind'] == 'native-thread'
    stages = {'initial', 'create_claimed', 'root_acked', 'body_claimed', 'done'} if threaded else {'initial', 'target_claimed', 'done'}
    if not isinstance(stage, str) or stage not in stages:
        raise ValueError('invalid strict native stage')
    if threaded:
        from plugins.platforms.discord.strict_thread import _validate_binding
        _validate_binding(binding, platform)
    elif binding['root'] is not None:
        raise ValueError('invalid strict native ordinary root')
    helper.validate_plan(binding)
    if stage in {'initial', 'create_claimed', 'target_claimed'}:
        if item['receipt'] is not None or item['receipt_sha256'] is not None:
            raise ValueError('unacknowledged strict native receipt')
    else:
        if item['receipt_sha256'] != digest({'binding': binding_hash, 'target': target, 'ack': item['receipt']}):
            raise ValueError('invalid strict native receipt fingerprint')
        if threaded:
            helper.validate_receipt(binding, item['receipt'])
        elif not isinstance(item['receipt'], dict) or item['receipt'].get('ok') is not True:
            raise ValueError('invalid strict native ordinary receipt')
    encode(binding)
    return stage


def deliver_thread(execution, index, state, frozen, item):
    from agent.async_utils import safe_schedule_threadsafe
    from cron import delivery_queue as queue, strict_thread_delivery as stages
    from cron.scheduler_delivery import _get_standalone_send_timeout
    helper = module(frozen['_native_platform'])
    plan = frozen['plan']
    if item['stage'] == 'initial':
        if not queue.claim_thread_create(execution, index):
            raise RuntimeError('strict native create claim lost')
        receipt = stages.await_fenced(safe_schedule_threadsafe(helper.create(plan), state.loop),
                                      _get_standalone_send_timeout())
        if not stages.transition(execution, index, 'create_claimed', 'root_acked', receipt):
            raise RuntimeError('strict native root receipt persistence lost')
        item['receipt'] = receipt
    helper.validate_receipt(plan['binding'], item['receipt'])
    if not queue.claim_thread_body(execution, index):
        raise RuntimeError('strict native body claim lost')
    stages.await_fenced(safe_schedule_threadsafe(helper.send(plan, item['receipt']), state.loop),
                        _get_standalone_send_timeout())
    safe_schedule_threadsafe(seed(state, plan['binding'], item['receipt']), state.loop).result(timeout=30)
    if not stages.transition(execution, index, 'body_claimed', 'done'):
        raise RuntimeError('strict native body settlement lost')


async def seed(state, binding, receipt):
    import asyncio
    from cron import scheduler_delivery as delivery
    platform, root = binding['platform'], receipt['root']
    adapter = state.runtime_adapter
    if platform == 'discord':
        import discord
        parent = adapter._client.get_channel(int(binding['parent']))
        if parent is None or not getattr(parent, 'guild', None):
            raise RuntimeError('strict native Discord continuation parent unavailable')
        response = receipt['ack']['response']
        thread = discord.Thread(guild=parent.guild, state=adapter._client._connection, data=response)
        parent.guild._add_thread(thread)
    if platform in {'discord', 'matrix'}:
        await adapter._threads.mark_async(root)
    ok = await asyncio.to_thread(delivery._seed_cron_session, state.job, adapter, platform,
        binding['parent'], state.mirror_text, thread_id=root,
        chat_type='thread' if platform == 'discord' else binding['chat_type'], user_id='system:cron',
        user_name='Cron', chat_name=state.origin.get('chat_name'), scope_id=binding['scope_id'],
        discord_keys_on_thread=True)
    if not ok:
        raise RuntimeError('strict native continuation seed failed after body ACK')
