"""Versioned create/body receipts in the existing cron delivery row.

Unclaimed creation and acknowledged roots are recoverable. A claimed mutation
without its durable ACK is uncertain forever, including after socket acceptance.
"""
import hashlib
import logging
import json

logger = logging.getLogger(__name__)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def await_fenced(future, timeout):
    """Wait for a scheduled send; on timeout stop the coroutine before it mutates again."""
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        try:
            future.result(timeout=5)
        except BaseException:  # observed, logged teardown of the abandoned send
            logger.debug('timed-out native send did not stop cleanly', exc_info=True)
        raise


def prepare_bundle(plans, job, content, for_failure):
    from hermes_constants import get_hermes_home
    from plugins.platforms.slack.strict_delivery import thread_binding
    staged = [calls for _, _, _, calls in plans if isinstance(calls, dict) and 'source' in calls]
    native = any(isinstance(calls, dict) and ('_native_platform' in calls or '_frozen_http_platform' in calls
                                            or '_prepared_transport' in calls)
                 for _, _, _, calls in plans)
    if not staged and not native and len(plans) < 2:
        return None
    mixed = native or len(staged) != len(plans)
    targets = []
    for index, (target, state, text, calls) in enumerate(plans):
        if isinstance(calls, dict) and '_native_platform' in calls:
            value = calls['plan']['binding']
            route = dict(target, _strict_payload_sha256=digest(value['messages']))
            targets.append({'kind': 'native-thread' if calls['new_thread'] else 'native', 'route': route, 'plan': value})
        elif isinstance(calls, dict) and '_frozen_http_platform' in calls:
            value = calls['plan']['binding']
            targets.append({'kind': 'live-http', 'route': target, 'plan': value})
        elif isinstance(calls, dict) and 'source' in calls:
            value = thread_binding(calls)
            route = dict(target, _strict_payload_sha256=digest(_plain(calls['entries'])))
            targets.append({'kind': 'thread', 'route': route, 'plan': value} if mixed else value)
        else:
            frozen, value = freeze_target(target, state, text, calls, job, content, for_failure)
            plans[index] = (target, state, text, frozen)
            targets.append(value)
    binding = {'version': 2 if mixed else 1, 'home': str(get_hermes_home().resolve()), 'job': job,
               'content_sha256': hashlib.sha256(content.encode()).hexdigest(), 'for_failure': bool(for_failure),
               'targets': targets}
    return validate({'binding': binding, 'binding_sha256': digest(binding),
                     'items': [{'stage': 'initial', 'receipt': None, 'receipt_sha256': None} for _ in plans]})


def _target(target):
    import re
    if (not isinstance(target, dict) or set(target) != {'workspace', 'parent', 'root', 'messages', 'media', 'transport_sha256'}
            or not isinstance(target['workspace'], str) or not re.fullmatch(r'T[A-Z0-9]+', target['workspace'])
            or not isinstance(target['parent'], str) or not re.fullmatch(r'[CDG][A-Z0-9]+', target['parent'])
            or not isinstance(target['transport_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', target['transport_sha256'])
            or not isinstance(target['root'], dict) or target['root'].get('channel') != target['parent']
            or not isinstance(target['root'].get('text'), str)
            or not isinstance(target['messages'], list) or not isinstance(target['media'], list)):
        raise ValueError('invalid strict thread target')
    for message in target['messages']:
        if not isinstance(message, dict) or message.get('channel') != target['parent'] or not isinstance(message.get('text'), str):
            raise ValueError('invalid strict thread message')
    for media in target['media']:
        if (not isinstance(media, list) or len(media) != 4 or not isinstance(media[0], str)
                or type(media[1]) is not bool or not isinstance(media[2], str)
                or not re.fullmatch('[0-9a-f]{64}', media[2]) or not isinstance(media[3], str)):
            raise ValueError('invalid strict thread media')


def _item(item, target, binding_hash):
    import re
    if not isinstance(item, dict) or set(item) != {'stage', 'receipt', 'receipt_sha256'}:
        raise ValueError('invalid strict thread stage schema')
    stage, receipt = item['stage'], item['receipt']
    if not isinstance(stage, str) or stage not in {'initial', 'create_claimed', 'root_acked', 'body_claimed', 'done'}:
        raise ValueError('invalid strict thread stage')
    if stage in {'initial', 'create_claimed'}:
        if receipt is not None or item['receipt_sha256'] is not None:
            raise ValueError('unclaimed strict thread receipt')
        return stage
    if (not isinstance(receipt, dict) or set(receipt) != {'ok', 'channel', 'ts', 'message'}
            or receipt['ok'] is not True or receipt['channel'] != target['parent']
            or not isinstance(receipt['ts'], str) or not re.fullmatch(r'[0-9]+\.[0-9]{6}', receipt['ts'])
            or not isinstance(receipt['message'], dict) or receipt['message'].get('text') != target['root']['text']
            or item['receipt_sha256'] != digest({'binding': binding_hash, 'target': target, 'ack': receipt})):
        raise ValueError('invalid strict thread root receipt')
    return stage


def validate(value):
    """Validate schema, stage, complete ACK and binding; never accept a bare thread id."""
    import re
    if not isinstance(value, dict) or set(value) != {'binding', 'binding_sha256', 'items'}:
        raise ValueError('invalid strict thread envelope')
    binding = value['binding']
    if (not isinstance(binding, dict) or set(binding) != {'version', 'home', 'job', 'content_sha256', 'for_failure', 'targets'}
            or type(binding['version']) is not int or binding['version'] not in (1, 2)
            or type(binding['for_failure']) is not bool or not isinstance(binding['home'], str)
            or not isinstance(binding['job'], dict) or not isinstance(binding['content_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', binding['content_sha256'])
            or value['binding_sha256'] != digest(binding)):
        raise ValueError('invalid strict thread binding')
    targets, items = binding['targets'], value['items']
    if not isinstance(targets, list) or not targets or not isinstance(items, list) or len(items) != len(targets):
        raise ValueError('invalid strict thread targets')
    unfinished = False
    for target, item in zip(targets, items):
        if binding['version'] == 1:
            _target(target)
            stage = _item(item, target, value['binding_sha256'])
        else:
            stage = _fanout_item(item, target, value['binding_sha256'])
            if target['kind'] == 'live-http':
                from cron.strict_live_http import validate_context
                validate_context(target, binding)
        if unfinished and stage != 'initial':
            raise ValueError('invalid strict thread ordering')
        unfinished = stage != 'done'

    return value


def bind_row(row, prepared):
    saved = row['thread_json']
    if saved is None:
        return prepared
    saved = validate(json.loads(saved))
    if prepared is None:
        raise ValueError('strict thread saved plan drift')
    if encode(saved['binding']) == encode(prepared['binding']):
        return saved
    validate(prepared)
    if saved['binding']['version'] != 2 or len(saved['binding']['targets']) != len(prepared['binding']['targets']):
        raise ValueError('strict thread saved plan drift')
    merged = json.loads(json.dumps(prepared))
    merged['items'] = json.loads(json.dumps(saved['items']))
    normalized = json.loads(json.dumps(prepared['binding']))
    for saved_target, candidate, item in zip(saved['binding']['targets'], normalized['targets'], saved['items']):
        if encode(saved_target) == encode(candidate):
            continue
        if saved_target['kind'] != 'bot-chat' or candidate['kind'] != 'bot-chat' or item['stage'] != 'initial':
            raise ValueError('strict thread saved plan drift')
        old = saved_target['plan']['identity']
        new = candidate['plan']['identity']
        old_endpoint, new_endpoint = old['endpoint'], new['endpoint']
        old_ref = old['params']['sealed_cron_result_v1']
        new_ref = new['params']['sealed_cron_result_v1']
        if (old_endpoint['instance_id'] != old_ref['owner_instance_id']
                or new_endpoint['instance_id'] != new_ref['owner_instance_id']):
            raise ValueError('strict thread saved plan drift')
        for key in ('instance_id', 'authority_epoch', 'api_origin'):
            new_endpoint[key] = old_endpoint[key]
        new_ref['owner_instance_id'] = old_ref['owner_instance_id']
        candidate['plan']['sha256'] = digest(new)
    if encode(saved['binding']) != encode(normalized):
        raise ValueError('strict thread saved plan drift')
    merged['binding_sha256'] = digest(merged['binding'])
    for target, item in zip(merged['binding']['targets'], merged['items']):
        if item['receipt'] is not None:
            item['receipt_sha256'] = digest({'binding': merged['binding_sha256'],
                                             'target': target, 'ack': item['receipt']})
    merged = validate(merged)
    return merged


def validate_row(row):
    value = validate(json.loads(row['thread_json']))
    binding = value['binding']
    from hermes_constants import get_hermes_home
    if (binding['home'] != str(get_hermes_home().resolve()) or encode(binding['job']) != encode(json.loads(row['job_json']))
            or binding['content_sha256'] != hashlib.sha256(row['content'].encode()).hexdigest()
            or type(row['for_failure']) is not int or row['for_failure'] not in (0, 1)
            or binding['for_failure'] != bool(row['for_failure'])
            or (binding['job'].get('execution_id') is not None
                and binding['job']['execution_id'] != row['execution_id'])):
        raise ValueError('strict thread queue provenance mismatch')
    return value


def recoverable(row):
    value = validate_row(row)
    return all(item['stage'] not in {'create_claimed', 'body_claimed', 'target_claimed'} for item in value['items'])


def transition(execution_id, index, before, after, receipt=None):
    from cron import delivery_queue as queue
    import os
    with queue._transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM deliveries WHERE execution_id=?', (execution_id,)).fetchone()
        if (row is None or row['status'] != 'delivering' or row['owner_process_id'] != queue._PROCESS_ID
                or row['owner_pid'] != os.getpid()):
            return False
        try:
            value = validate_row(row)
        except ValueError:
            return False
        if type(index) is not int or not 0 <= index < len(value['items']):
            return False
        if any(item['stage'] != 'done' for item in value['items'][:index]):
            return False
        item = value['items'][index]
        if item['stage'] != before:
            return False
        target = value['binding']['targets'][index]
        kind = target.get('kind', 'thread') if value['binding']['version'] == 2 else 'thread'
        allowed = ({('initial', 'create_claimed'), ('create_claimed', 'root_acked'),
                    ('root_acked', 'body_claimed'), ('body_claimed', 'done')} if kind in {'thread', 'native-thread'}
                   else {('initial', 'target_claimed'), ('target_claimed', 'initial'), ('target_claimed', 'done')})
        if kind == 'bot-chat':
            allowed |= {('target_claimed', 'bot_waiting'), ('bot_waiting', 'done')}
        if (before, after) not in allowed:
            return False
        item['stage'] = after
        if after == 'initial':
            item['receipt'] = None
            item['receipt_sha256'] = None
        elif receipt is not None:
            item['receipt'] = receipt
            item['receipt_sha256'] = digest({'binding': value['binding_sha256'],
                                           'target': value['binding']['targets'][index], 'ack': receipt})
        validate(value)
        conn.execute('UPDATE deliveries SET thread_json=? WHERE execution_id=?', (encode(value), execution_id))
    return True


def _deliver_thread(execution, index, state, frozen, item):
    from cron import delivery_queue as queue, scheduler_delivery as delivery
    from agent.async_utils import safe_schedule_threadsafe
    from plugins.platforms.slack.strict_delivery import create_thread, send_thread_body
    if item['stage'] == 'initial':
        if not queue.claim_thread_create(execution, index):
            raise RuntimeError('strict thread create claim lost')
        receipt = safe_schedule_threadsafe(create_thread(frozen), state.loop).result(timeout=30)
        if not transition(execution, index, 'create_claimed', 'root_acked', receipt):
            raise RuntimeError('strict thread root receipt persistence lost')
        item['receipt'] = receipt
    root = item['receipt']['ts']
    if not queue.claim_thread_body(execution, index):
        raise RuntimeError('strict thread body claim lost')
    safe_schedule_threadsafe(send_thread_body(frozen, root), state.loop).result(timeout=delivery._get_standalone_send_timeout())
    ok = delivery._seed_cron_session(state.job, state.runtime_adapter, 'slack', frozen['parent'], state.mirror_text,
        thread_id=root, chat_type='dm' if frozen['parent'].startswith('D') else 'group',
        user_id='system:cron', user_name='Cron', chat_name=state.origin.get('chat_name'), scope_id=frozen['workspace'])
    if not ok:
        raise RuntimeError('strict thread continuation seed failed after body ACK')
    if not transition(execution, index, 'body_claimed', 'done'):
        raise RuntimeError('strict thread body settlement lost')


def deliver(execution, plans):
    from cron import delivery_queue as queue
    from hermes_constants import get_hermes_home
    from cron.strict_delivery import DeferredDelivery
    saved = validate_row(queue.get_status(execution))
    if saved['binding']['home'] != str(get_hermes_home().resolve()):
        raise RuntimeError('strict thread profile changed after claim')
    for index, (_, state, _, frozen) in enumerate(plans):
        item = saved['items'][index]
        if item['stage'] == 'done':
            continue
        target = saved['binding']['targets'][index]
        kind = target['kind'] if saved['binding']['version'] == 2 else 'thread'
        if kind == 'thread':
            _deliver_thread(execution, index, state, frozen, item)
        elif kind == 'native-thread':
            from cron.strict_native_delivery import deliver_thread
            deliver_thread(execution, index, state, frozen, item)
        elif kind == 'bot-chat' and item['stage'] == 'bot_waiting':
            if frozen.get('receipt') is None:
                raise RuntimeError('strict Bot receipt missing after admission; do not resend')
            receipt = send_target(state, frozen)
            if not transition(execution, index, 'bot_waiting', 'done', receipt):
                raise RuntimeError('strict Bot deferred settlement lost')
        else:
            if not queue.claim_fanout_target(execution, index):
                raise RuntimeError('strict fanout target claim lost')
            try:
                receipt = send_target(state, frozen)
            except DeferredDelivery as exc:
                after = 'bot_waiting' if kind == 'bot-chat' else 'initial'
                if not transition(execution, index, 'target_claimed', after, exc.receipt):
                    raise RuntimeError('strict fanout deferred target reset lost')
                raise
            if not transition(execution, index, 'target_claimed', 'done', receipt):
                raise RuntimeError('strict fanout target settlement lost')


def _fanout_item(item, target, binding_hash):
    import re
    if (not isinstance(target, dict) or set(target) != {'kind', 'route', 'plan'}
            or not isinstance(target['kind'], str) or not isinstance(target['route'], dict)
            or not isinstance(target['plan'], dict)):
        raise ValueError('invalid strict fanout target')
    kind = target['kind']
    if kind == 'live-http':
        from cron.strict_live_http import validate as validate_live_http
        return validate_live_http(item, target, binding_hash)
    if kind in {'native-thread', 'native'}:
        from cron.strict_native_delivery import validate_item
        return validate_item(item, target, binding_hash)
    if kind == 'thread':
        _target(target['plan'])
        # The ACK includes the whole per-target route binding in version 2.
        probe = dict(item) if isinstance(item, dict) else item
        if isinstance(probe, dict) and probe.get('receipt') is not None:
            if probe.get('receipt_sha256') != digest({'binding': binding_hash, 'target': target, 'ack': probe['receipt']}):
                raise ValueError('invalid strict fanout root receipt')
            probe['receipt_sha256'] = digest({'binding': binding_hash, 'target': target['plan'], 'ack': probe['receipt']})
        return _item(probe, target['plan'], binding_hash)
    fields = {'sha256', 'identity'} if kind == 'bot-chat' else {'sha256'}
    if (kind not in {'slack', 'telegram', 'standalone', 'local', 'room', 'bot-chat', 'prepared'} or set(target['plan']) != fields
            or not isinstance(target['plan']['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', target['plan']['sha256'])):
        raise ValueError('invalid strict fanout plan')
    if kind == 'bot-chat' and digest(target['plan']['identity']) != target['plan']['sha256']:
        raise ValueError('invalid strict Bot plan identity')
    if not isinstance(item, dict) or set(item) != {'stage', 'receipt', 'receipt_sha256'}:
        raise ValueError('invalid strict fanout item')
    stage = item['stage']
    if not isinstance(stage, str) or stage not in ({'initial', 'target_claimed', 'bot_waiting', 'done'}
                                                   if kind == 'bot-chat' else {'initial', 'target_claimed', 'done'}):
        raise ValueError('invalid strict fanout stage')
    if stage == 'bot_waiting':
        receipt = item['receipt']
        params = target['plan']['identity']['params']
        if (not isinstance(receipt, dict) or set(receipt) != {'ok', 'delivery_id', 'session_id', 'admission_id'}
                or receipt['ok'] is not True or receipt['delivery_id'] != params['id']
                or receipt['session_id'] != params['session_id']
                or not isinstance(receipt['admission_id'], str) or not receipt['admission_id']
                or item['receipt_sha256'] != digest({'binding': binding_hash, 'target': target, 'ack': receipt})):
            raise ValueError('invalid strict Bot waiting receipt')
    elif stage != 'done':
        if item['receipt'] is not None or item['receipt_sha256'] is not None:
            raise ValueError('unacknowledged strict fanout receipt')
    elif (not isinstance(item['receipt'], dict) or item['receipt'].get('ok') is not True
            or item['receipt_sha256'] != digest({'binding': binding_hash, 'target': target, 'ack': item['receipt']})):
        raise ValueError('invalid strict fanout receipt')
    return stage


def _plain(value):
    """Stable fingerprints of the frozen payload; never persist credential bytes."""
    import dataclasses
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, bytes):
        return {'bytes_sha256': hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {key: _plain(part) for key, part in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(part) for part in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def freeze_target(target, state, text, calls, job, content, failure):
    from tools.send_message_strict import PreparedPlan
    if isinstance(calls, dict) and calls.get('_prepared_transport') is True:
        frozen, identity = calls, calls['identity']
    elif isinstance(calls, PreparedPlan):
        with calls._consumption.lock:
            payload = calls._consumption.payload
            if payload is None:
                raise RuntimeError('strict fanout standalone already attempted')
            identity = _plain(payload)
        frozen = {'kind': 'standalone', 'calls': calls}
    elif state is not None and state.platform_name == 'slack':
        frozen, identity = _freeze_slack(state, calls, content, failure)
    elif state is not None and state.platform_name == 'telegram':
        frozen, identity = _freeze_telegram(state, calls, content, failure)
    elif state is None and target['platform'] == 'local':
        frozen, identity = _freeze_local(target, text, failure)
    elif state is None and target['platform'] == 'room':
        frozen, identity = _freeze_room(target, job, content, failure)
    elif state is None and target['platform'] == 'bot-chat':
        from cron.strict_bot_delivery import freeze
        frozen, identity = freeze(target, job, content, failure)
    else:
        raise RuntimeError('strict fanout frozen native target unavailable: ' + target['platform'])
    descriptor = {'kind': frozen['kind'], 'route': target, 'plan': {'sha256': digest(identity)}}
    if frozen['kind'] == 'bot-chat':
        descriptor['plan']['identity'] = identity
    return frozen, descriptor


def _freeze_telegram(state, calls, content, failure):
    from pathlib import Path
    from telegram import Bot
    from plugins.platforms.telegram.adapter import normalize_telegram_chat_id, utf16_len
    from plugins.platforms.telegram.strict_delivery import preflight, media_routing
    from gateway.platforms.base import resolve_proxy_url
    adapter = state.runtime_adapter
    if not isinstance(adapter._bot, Bot):
        raise RuntimeError('strict fanout Telegram Bot unavailable')
    operations, media = [], []
    for method, kwargs in calls:
        preflight(adapter, method, chat_id=state.chat_id, **kwargs)
        metadata = kwargs.get('metadata')
        if method == 'send':
            thread = adapter._metadata_thread_id(metadata)
            chunks = adapter.truncate_message(adapter.format_message(kwargs['content']), adapter.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
            for index, chunk in enumerate(chunks):
                _private, _off, anchor = adapter._chunk_reply_routing(state.chat_id, None, metadata, thread, index)
                routing = adapter._thread_kwargs_for_send(state.chat_id, thread, metadata,
                    reply_to_message_id=anchor, reply_to_mode=adapter._reply_to_mode)
                operations.append(('message', dict(chat_id=normalize_telegram_chat_id(state.chat_id), text=chunk,
                    parse_mode='MarkdownV2', reply_to_message_id=anchor, **routing,
                    **adapter._link_preview_kwargs(), **adapter._notification_kwargs(metadata))))
        else:
            key, kind = {'send_document': ('file_path', 'document'), 'send_image_file': ('image_path', 'photo'),
                         'send_video': ('video_path', 'video'), 'send_voice': ('audio_path', 'voice')}[method]
            path = Path(kwargs[key])
            data, name = path.read_bytes(), kwargs.get('file_name') or path.name
            if kind == 'voice' and path.suffix.lower() not in {'.ogg', '.opus'}:
                kind = 'audio' if path.suffix.lower() in {'.mp3', '.m4a'} else 'document'
            media.append([str(path), kwargs.get('is_voice', False), hashlib.sha256(data).hexdigest(), name])
            operations.append((kind, dict(media_routing(adapter, state.chat_id, None, metadata),
                payload=(data, name), caption=kwargs.get('caption'))))
    from cron.strict_delivery import _durable_media
    if media != _durable_media(state.job, content, failure)[1]:
        raise RuntimeError('strict fanout Telegram media differs from durable result')
    source = adapter._bot
    options = {'token': source.token, 'base_url': source.base_url, 'base_file_url': source.base_file_url,
        'proxy': resolve_proxy_url('TELEGRAM_PROXY', target_hosts=['api.telegram.org'], configured=adapter.config.extra.get('proxy_url'))}
    return {'kind': 'telegram', 'operations': operations, 'options': options}, _plain((options, operations))


def _freeze_slack(state, calls, content, failure):
    import copy
    from pathlib import Path
    from plugins.platforms.slack.strict_delivery import prepare, preflight, thread_binding
    from plugins.platforms.slack.adapter import _slack_unfurl_kwargs
    adapter = state.runtime_adapter
    messages, entries, media = [], [], []
    metadata = calls[0][1].get('metadata')
    parent = preflight(adapter, str(state.chat_id), metadata)
    for method, kwargs in calls:
        prepare(adapter, method, chat_id=parent, **kwargs)
        if method == 'send':
            chunks = adapter.truncate_message(adapter.format_message(kwargs['content']), adapter.MAX_MESSAGE_LENGTH)
            blocks = adapter._maybe_blocks(kwargs['content']) if len(chunks) == 1 else None
            for index, chunk in enumerate(chunks):
                message = dict(channel=parent, text=chunk, mrkdwn=True, **_slack_unfurl_kwargs(adapter.config.extra))
                if blocks and index == 0:
                    message['blocks'] = blocks
                if state.thread_id:
                    message['thread_ts'] = str(state.thread_id)
                    if adapter.config.extra.get('reply_broadcast', False) and index == 0:
                        message['reply_broadcast'] = True
                messages.append(message)
        else:
            key = {'send_document': 'file_path', 'send_image_file': 'image_path',
                   'send_video': 'video_path', 'send_voice': 'audio_path'}.get(method)
            if key is None:
                raise RuntimeError('strict fanout Slack media method unavailable: ' + method)
            path = kwargs[key]
            data = Path(path).read_bytes()
            name = kwargs.get('file_name') or Path(path).name
            entries.append((data, name, kwargs.get('caption') or ''))
            media.append([path, kwargs.get('is_voice', False), hashlib.sha256(data).hexdigest(), name])
    from cron.strict_delivery import _durable_media
    if media != _durable_media(state.job, content, failure)[1]:
        raise RuntimeError('strict fanout frozen media differs from durable result')
    source = copy.copy(adapter._client_for(parent, metadata))
    source.headers, source.default_params = copy.deepcopy(source.headers), copy.deepcopy(source.default_params)
    if source.default_params:
        raise RuntimeError('strict fanout Slack default parameters unsupported')
    frozen = {'kind': 'slack', 'source': source, 'root': {'channel': parent, 'text': ''},
              'workspace': adapter._metadata_team_id(metadata) or adapter.scope_id_for_chat(parent),
              'parent': parent, 'messages': messages, 'entries': entries, 'media': media, 'thread': state.thread_id}
    return frozen, dict(thread_binding(frozen), entries=_plain(entries), thread=state.thread_id)


def _freeze_local(target, text, failure):
    from gateway.session_cron import owner_for_home
    from gateway.session_cron_delivery import snapshot
    from hermes_constants import get_hermes_home
    owner = owner_for_home(get_hermes_home())
    if owner is None:
        raise RuntimeError('strict fanout local owner unavailable')
    authority, loop = owner
    chat = target['chat_id']
    identity = dict(snapshot(authority, chat), text=text, failure=failure)
    return {'kind': 'local', 'authority': authority, 'epoch': authority.epoch, 'loop': loop, 'identity': identity}, identity


def _freeze_room(target, job, content, failure):
    from cron.scheduler_delivery import _redact_cron_payload
    from cron.scheduler_delivery_room import _ROOM_TOKEN_RE, _find_room_store
    from gateway.hosted_room_context import prepare_scheduled_message
    from hermes_cli.profiles import profile_exists
    match = _ROOM_TOKEN_RE.fullmatch(target['chat_id'])
    if match is None or (match['profile'] and not profile_exists(match['profile'])):
        raise RuntimeError('strict room target invalid')
    store = _find_room_store(match['room'], match['profile'])
    if store is None or not job.get('execution_id'):
        raise RuntimeError('strict room target unavailable')
    store = store.resolve()
    plan = prepare_scheduled_message(store, room_id=match['room'], job_id=str(job.get('id') or 'job'),
        job_name=_redact_cron_payload(str(job.get('name') or job.get('id') or 'job'), 'job name'),
        run_key=job['execution_id'], text=_redact_cron_payload(content, 'room payload'), notice=failure)
    stat = store.stat()
    identity = {'store': str(store), 'inode': [stat.st_dev, stat.st_ino], 'event': plan}
    return {'kind': 'room', 'identity': identity}, identity


def send_target(state, frozen):
    from agent.async_utils import safe_schedule_threadsafe
    from cron import scheduler_delivery as delivery
    kind = frozen['kind']
    if kind == 'prepared':
        from cron.strict_prepared_transport import send
        return send(state, frozen)
    if kind == 'live-http':
        from cron.strict_live_http import send
        receipt = send(state, frozen)
        delivery._seed_live_delivery_sessions(state, receipt.get('message_id'))
        return receipt
    if kind == 'native':
        from cron.strict_native_delivery import module
        from cron.strict_thread_delivery import await_fenced
        receipt = await_fenced(safe_schedule_threadsafe(module(frozen['_native_platform']).send(frozen['plan']),
                                                        state.loop), delivery._get_standalone_send_timeout())
        binding = frozen['plan']['binding']
        if binding.get('root') is not None or binding.get('thread_id') is not None:
            delivery._seed_live_delivery_sessions(state, receipt['message_id'])
        return receipt
    if kind == 'bot-chat':
        from cron.strict_bot_delivery import send
        return send(frozen)
    if kind == 'room':
        from pathlib import Path
        from gateway.hosted_rooms import append_event
        identity = frozen['identity']
        stat = Path(identity['store']).stat()
        if [stat.st_dev, stat.st_ino] != identity['inode']:
            raise RuntimeError('strict room store changed after claim')
        plan = identity['event']
        event = append_event(identity['store'], **plan)
        if any(event.get(key) != plan[key] for key in ('room_id', 'event_id', 'kind', 'actor', 'payload', 'authority_epoch')):
            raise RuntimeError('strict room append receipt mismatch')
        return {'ok': True, 'event_id': event['event_id'], 'seq': event['seq']}
    if kind == 'standalone':
        from cron.strict_delivery import standalone_send
        calls = frozen['calls']
        result, error = standalone_send(state, calls.binding.message, calls.binding.media,
            prepared_plan=calls, execution_id=calls.binding.execution_id, claim_id=calls.binding.claim_id)
        if error:
            raise RuntimeError(error)
        delivery._maybe_mirror_cron_delivery(state.job, state.platform_name, state.chat_id,
            state.mirror_text, thread_id=state.thread_id, user_id=state.origin_user_id, enabled=state.mirror_this_target)
        return {'ok': True, 'message_id': result.get('message_id')}
    if kind == 'local':
        from gateway.session_cron_delivery import admit
        identity = frozen['identity']
        result = safe_schedule_threadsafe(admit(frozen['authority'], identity, frozen['epoch']), frozen['loop']).result(timeout=60)
        if not result:
            raise RuntimeError('strict fanout local admission refused')
        return {'ok': True}
    send = _send_telegram if kind == 'telegram' else _send_slack
    last_id = safe_schedule_threadsafe(send(frozen), state.loop).result(timeout=delivery._get_standalone_send_timeout())
    delivery._seed_live_delivery_sessions(state, last_id)
    return {'ok': True, 'message_id': last_id}


async def _send_telegram(frozen):
    import httpx
    from telegram import Bot, InputFile
    from telegram.request import HTTPXRequest
    from agent.ssl_verify import platform_ssl_context
    options = frozen['options']
    transport = httpx.AsyncHTTPTransport(retries=0, proxy=options['proxy'], verify=platform_ssl_context())
    request = HTTPXRequest(httpx_kwargs={'transport': transport, 'follow_redirects': False, 'trust_env': False})
    bot = Bot(options['token'], base_url=lambda token: options['base_url'],
              base_file_url=lambda token: options['base_file_url'], request=request, get_updates_request=request)
    last_id = None
    try:
        for method, saved in frozen['operations']:
            kwargs = dict(saved)
            if 'payload' in kwargs:
                data, name = kwargs.pop('payload')
                kwargs[method] = InputFile(data, filename=name)
            message = await getattr(bot, 'send_' + method)(**kwargs)
            last_id = str(message.message_id)
    finally:
        await request.shutdown()
    return last_id


async def _send_slack(frozen):
    from plugins.platforms.slack.strict_delivery import single_client
    last_id = None
    async with single_client(None, None, None, source=frozen['source']) as client:
        for message in frozen['messages']:
            result = await client.chat_postMessage(**message)
            if not result.get('ts') or result.get('channel') != frozen['parent']:
                raise RuntimeError('strict fanout Slack ACK missing')
            last_id = result['ts']
        entries = frozen['entries']
        for start in range(0, len(entries), 10):
            batch = entries[start:start + 10]
            result = await client.files_upload_v2(channel=frozen['parent'], thread_ts=frozen['thread'],
                file_uploads=[{'content': data, 'filename': name} for data, name, caption in batch],
                initial_comment='\n'.join(caption for data, name, caption in batch if caption))
            files = result.get('files') or []
            if len(files) != len(batch) or any(not entry.get('id') for entry in files):
                raise RuntimeError('strict fanout Slack media ACK missing')
    return last_id