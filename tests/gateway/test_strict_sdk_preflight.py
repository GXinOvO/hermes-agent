from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gateway import background_messaging_results as durable
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.strict_transport import prepare_strict_transport
from hermes_state import SessionDB
from hermes_state_background import _key, _load
from tests.gateway import test_strict_sdk_delivery as sdk


@asynccontextmanager
async def connected(name, wire, monkeypatch):
    factory = sdk.telegram_adapter(wire) if name == 'telegram' else (
        sdk.discord_adapter(wire, monkeypatch) if name == 'discord' else sdk.slack_adapter(wire))
    async with factory as adapter:
        yield adapter, 'C555' if name == 'slack' else '555'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
async def test_later_missing_media_keeps_whole_background_delivery_pending(name, tmp_path, monkeypatch):
    async with sdk.peer() as wire, connected(name, wire, monkeypatch) as (adapter, chat):
        db = SessionDB(tmp_path / 'state.db')
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:adapter)
        source = SessionSource(platform=Platform(name), chat_id=chat)
        try:
            context = await durable.prepare(runner, 'work', source, 'missing_bundle', [], [])
            path = tmp_path / 'proof.png'
            path.write_bytes(b'proof')
            await durable.seal(context, 'missing_bundle', 'sealed text\nMEDIA:' + str(path))
            plan = durable.delivery_plan(adapter, source, {'text':'sealed text\nMEDIA:' + str(path)})
            assert [method for method, _ in plan] == ['send', 'send_image_file']
            path.unlink()
            with pytest.raises(RuntimeError, match='strict.*(media|payload)'):
                await durable.prepare_plan(adapter, plan)
            assert await durable.recover(runner, db) == 0
            assert wire.events == []
            assert db._execute_write(lambda conn:_load(conn, _key('missing_bundle')))['delivery'] == 'pending'
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
async def test_static_sdk_refusal_does_not_spend_delivery_claim(name, tmp_path, monkeypatch):
    async with sdk.peer() as wire, connected(name, wire, monkeypatch) as (adapter, chat):
        if name == 'telegram':
            kwargs = {'images':[('https://inert.invalid/image.gif', 'caption')]}
            method = 'send_multiple_images'
        elif name == 'discord':
            monkeypatch.setattr(adapter, '_is_forum_parent', lambda channel:True)
            kwargs, method = {'content':'sealed'}, 'send'
        else:
            chat = 'U555'
            kwargs, method = {'content':'sealed'}, 'send'
        with pytest.raises(RuntimeError, match='strict.*(unsupported|unavailable|payload)'):
            await prepare_strict_transport(adapter, method, chat_id=chat, **kwargs)
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
async def test_sdk_preflight_accepts_current_valid_target_without_post(name, tmp_path, monkeypatch):
    path = tmp_path / 'proof.png'
    path.write_bytes(b'proof')
    async with sdk.peer() as wire, connected(name, wire, monkeypatch) as (adapter, chat):
        await prepare_strict_transport(adapter, 'send', chat_id=chat, content='sealed')
        await prepare_strict_transport(adapter, 'send_image_file', chat_id=chat, image_path=str(path))
        assert wire.events == []
