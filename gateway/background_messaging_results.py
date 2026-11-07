"""Result persistence for messaging /bg; an unknown send is never retried."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


async def prepare(runner, prompt, source, task_id, media_urls, media_types):
    from gateway.session_authorities import active_authority
    from hermes_state import SessionDB
    from hermes_state_background import begin
    authority = active_authority(runner)
    db = authority.db if authority is not None else getattr(runner, '_session_db', None)
    db = getattr(db, '_db', db)
    opened = db is None
    if opened:
        db = await asyncio.to_thread(SessionDB)
    identity = {'profile_id':str(Path(db.db_path).parent.resolve()), 'parent_session_id':'messaging-background',
                'event_type':'background.complete', 'source':source.to_dict(),
                'input_sha256':hashlib.sha256(json.dumps([prompt,media_urls or [],media_types or []],sort_keys=True).encode()).hexdigest()}
    if authority is not None:
        identity['owner_epoch'] = authority.epoch
    try:
        record = await asyncio.to_thread(begin, db, task_id, identity)
        if not record.get('claimed'):
            raise ValueError('background execution already exists: ' + record['status'])
        return db, identity, opened
    except BaseException:
        if opened:
            db.close()
        raise


async def seal(context, task_id, response, header='', metadata=None):
    from hermes_state_background import complete
    db, identity, _ = context
    await asyncio.to_thread(complete, db, task_id, identity,
                           {'task_id':task_id, 'text':str(response or ''), 'header':header, 'metadata':metadata})


async def claim(context, task_id):
    from hermes_state_background import claim_delivery
    db = context[0]
    if not await asyncio.to_thread(claim_delivery, db, task_id):
        raise ValueError('background delivery is already claimed; reconcile before retrying')


async def finish(context, task_id, outcome):
    from hermes_state_background import finish_delivery
    await asyncio.to_thread(finish_delivery, context[0], task_id, outcome)


async def _sent(operation):
    result = await operation
    if not getattr(result, 'success', False):
        raise RuntimeError('background delivery acknowledgement missing')


def delivery_plan(adapter, source, payload):
    from agent.i18n import t
    from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio
    from gateway.run_notifications import _IMAGE_EXTS, _VIDEO_EXTS
    from gateway.strict_transport import require_strict_transport
    media, response = adapter.extract_media(payload['text'])
    checked_media = BasePlatformAdapter.filter_media_delivery_paths(media)
    if len(checked_media) != len(media):
        raise RuntimeError('strict media payload unavailable before delivery')
    media = checked_media
    images, text = adapter.extract_images(response)
    metadata = payload.get('metadata')
    header = payload.get('header', '')
    operations = []
    if text or not images and not media:
        operations.append(('send', {'content':header + (text or t('gateway.background.no_response'))}))
    for url, alt in images or []:
        operations.append(('send_image', {'image_url':url, 'caption':alt}))
    for path, voice in media or []:
        ext = os.path.splitext(path)[1].lower()
        if should_send_media_as_audio(source.platform, ext, voice):
            operations.append(('send_voice', {'audio_path':path, 'is_voice':voice}))
        else:
            sender, key = (('send_video', 'video_path') if ext in _VIDEO_EXTS else
                           ('send_image_file', 'image_path') if ext in _IMAGE_EXTS else
                           ('send_document', 'file_path'))
            operations.append((sender, {key:path}))
    for method, kwargs in operations:
        require_strict_transport(adapter, method)
        kwargs.update(chat_id=source.chat_id, metadata=metadata)
    return operations


async def deliver_plan(adapter, plan):
    from gateway.strict_transport import strict_call
    for method, kwargs in plan:
        await _sent(strict_call(adapter, method, **kwargs))


async def prepare_plan(adapter, plan):
    from gateway.strict_transport import prepare_strict_transport
    for method, kwargs in plan:
        await prepare_strict_transport(adapter, method, **kwargs)


async def deliver(adapter, source, payload):
    await deliver_plan(adapter, delivery_plan(adapter, source, payload))


async def recover(runner, db):
    from gateway.session import SessionSource
    from hermes_state_background import pending_deliveries, claim_delivery
    delivered = 0
    for record in await asyncio.to_thread(pending_deliveries, db):
        if record['parent_session_id'] != 'messaging-background':
            continue
        try:
            origin = record['binding']['source']
            if not isinstance(origin, dict) or not isinstance(origin.get('chat_id'), str) or not origin['chat_id']:
                raise ValueError('invalid background messaging source')
            source = SessionSource.from_dict(origin)
            adapter = runner._delivery_adapter_for(source)
            if adapter is None:
                continue
            plan = delivery_plan(adapter, source, record['payload'])
            await prepare_plan(adapter, plan)
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, OverflowError):
            logger.warning('Background result %s transport unavailable', record['task_id'], exc_info=True)
            continue
        if not await asyncio.to_thread(claim_delivery, db, record['task_id']):
            continue
        context = (db, record['binding'], False)
        try:
            await deliver_plan(adapter, plan)
        except BaseException as exc:
            await asyncio.shield(finish(context, record['task_id'], 'unknown'))
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning('Background result %s delivery unknown', record['task_id'], exc_info=True)
        else:
            await finish(context, record['task_id'], 'delivered')
            delivered += 1
    return delivered


async def recover_served(runner):
    from gateway.session_authorities import serving_authorities, async_owner_scope
    for authority in serving_authorities(runner):
        async with async_owner_scope(authority):
            await recover(runner, authority.db)


def close(context):
    if context is not None and context[2]:
        context[0].close()
