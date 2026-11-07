import asyncio
from datetime import datetime, UTC
import os
from types import SimpleNamespace
import time


async def check(adapter, config, platform, mode):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    store = adapter._session_store
    os.environ['GATEWAY_ALLOWED_USERS'] = '999,@human:local'
    os.environ['TELEGRAM_ALLOWED_USERS'] = '999'
    os.environ['DISCORD_HISTORY_BACKFILL'] = 'false'
    entries = list(store._entries.items())
    if not entries:
        raise AssertionError('native continuation seed missing')
    key, entry = entries[0]
    seeded = entry.session_id
    if mode == 'reset':
        store.reset_session(key)
    elif mode == 'ended':
        store._db.end_session(seeded, 'completed')
    runner = object.__new__(GatewayRunner)
    runner.adapters, runner.config, runner.pairing_store = {Platform(platform): adapter}, config, None
    auth, routes = [], []

    def authorized(user, chat_type, chat_id, **kwargs):
        source = SessionSource(platform=Platform(platform), chat_id=chat_id, chat_type=chat_type,
                               user_id=user, thread_id=kwargs.get('thread_id'))
        verdict = runner._is_user_authorized(source)
        auth.append({'user': user, 'allowed': verdict})
        return verdict

    async def handle(event):
        verdict = runner._is_user_authorized(event.source)
        auth.append({'user': event.source.user_id, 'allowed': verdict})
        if verdict:
            target = store.get_or_create_session(event.source)
            routes.append({'source': event.source.to_dict(), 'key': store._generate_session_key(event.source),
                           'id': target.session_id, 'transcript': store.load_transcript(target.session_id),
                           'channel_context': event.channel_context})

    adapter.set_authorization_check(authorized)
    adapter.set_message_handler(handle)
    adapter._text_batch_delay_seconds = 0
    adapter.config.typing_indicator = False
    if platform == 'discord':
        import discord
        from tests.gateway.strict_native_thread_support import _discord
        parent = adapter._client.get_channel(555)
        thread = discord.Thread(guild=parent.guild, state=adapter._client._connection,
                                data=_discord('/channels/555/threads', {'name': 'Hermes native'}, None, 1))
        parent.guild._add_thread(thread)
        user = SimpleNamespace(id=987 if mode == 'unauthorized' else 999, name='Human', display_name='Human', bot=False)
        message = SimpleNamespace(id=5000, content='continue native saved brief', channel=thread, guild=parent.guild,
            author=user, mentions=[], attachments=[], reference=None, message_snapshots=[],
            created_at=datetime.now(UTC), type=discord.MessageType.default)
        await adapter._handle_message(message)
    elif platform == 'matrix':
        adapter._user_id = '@bot:local'
        adapter._startup_ts = time.time() - 10
        room = '!room:local'
        adapter._room_identities[room] = SimpleNamespace(display_name='Native room', room_topic=None,
            server_name='local', chat_type='room')
        adapter._room_identity_cached_at[room] = time.monotonic()
        sender = '@blocked:local' if mode == 'unauthorized' else '@human:local'
        message = SimpleNamespace(room_id=room, sender=sender, event_id='$human', timestamp=int(time.time() * 1000),
            content={'msgtype': 'm.text', 'body': 'continue native saved brief',
                     'm.relates_to': {'rel_type': 'm.thread', 'event_id': '$root'}})
        await adapter._on_room_message(message)
    else:
        from telegram import Update, User
        adapter._bot._bot_user = User(id=123, is_bot=True, first_name='Bot', username='hermes_inert')
        adapter._forum_command_registered.add(555)
        user = {'id': 987 if mode == 'unauthorized' else 999, 'is_bot': False, 'first_name': 'Human'}
        raw = {'update_id': 8000, 'message': {'message_id': 5000, 'date': int(time.time()), 'text': 'continue native saved brief',
            'from': user, 'chat': {'id': 555, 'type': 'supergroup', 'title': 'Native forum', 'is_forum': True},
            'message_thread_id': 777, 'is_topic_message': True,
            'reply_to_message': {'message_id': 1000, 'date': int(time.time()),
                'chat': {'id': 555, 'type': 'supergroup', 'title': 'Native forum', 'is_forum': True},
                'from': {'id': 123, 'is_bot': True, 'first_name': 'Bot', 'username': 'hermes_inert'},
                'text': 'SEALED_NATIVE_CRON_RESULT', 'message_thread_id': 777, 'is_topic_message': True}}}
        await adapter._handle_text_message(Update.de_json(raw, adapter._bot), None)
    batches = list(adapter._pending_text_batch_tasks.values())
    if batches:
        await asyncio.wait_for(asyncio.gather(*batches), 20)
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
    return {'seeded': seeded, 'seeded_key': key, 'routes': routes, 'auth': auth}
