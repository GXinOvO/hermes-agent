from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_background_preflight_hands_back_validated_media_before_open(tmp_path, monkeypatch):
    from gateway import background_messaging_results as durable
    from gateway.platforms import docker_media_paths

    path = tmp_path / 'report.txt'
    handback = AsyncMock(side_effect=lambda paths: path.write_text('available'))
    monkeypatch.setattr(docker_media_paths, 'hand_back_unreadable_media', handback)
    adapter = SimpleNamespace(send_document=single_attempt_transport(AsyncMock()))
    plan = [('send_document', {'chat_id':'chat', 'file_path':str(path), 'metadata':None})]

    await durable.prepare_plan(adapter, plan)

    handback.assert_awaited_once_with([str(path)])


@pytest.mark.asyncio
async def test_background_recovery_retries_preflight_without_spending_delivery_claim(tmp_path, monkeypatch):
    from gateway import background_messaging_results as durable
    from gateway.platforms import docker_media_paths

    path = tmp_path / 'report.txt'
    path.write_text('report')
    calls = []
    attempts = 0

    async def handback(paths):
        nonlocal attempts
        calls.append(('handback', paths))
        attempts += 1
        if attempts == 1:
            raise RuntimeError('sandbox temporarily unavailable')

    async def send_document(**kwargs):
        calls.append(('send', kwargs['file_path']))
        return SendResult(success=True)

    monkeypatch.setattr(docker_media_paths, 'hand_back_unreadable_media', handback)
    monkeypatch.setattr(BasePlatformAdapter, 'filter_media_delivery_paths', staticmethod(lambda paths: paths))
    adapter = SimpleNamespace(
        extract_media=lambda text: ([(str(path), False)], ''),
        extract_images=lambda text: ([], text),
        send_document=single_attempt_transport(send_document),
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'bg_media', [], [])
        await durable.seal(context, 'bg_media', 'MEDIA:' + str(path))

        assert await durable.recover(runner, db) == 0
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0

    assert calls == [('handback', [str(path)]), ('handback', [str(path)]), ('send', str(path))]


@pytest.mark.asyncio
async def test_background_recovery_uses_preclaim_plan_once(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, _load

    class Adapter:
        def __init__(self):
            self.prepared = []
            self.sent = []

        def extract_media(self, text):
            return [], text

        def extract_images(self, text):
            return [], text

        async def _prepare_strict_transport(self, method, *, execution_id, claim_id, **kwargs):
            assert method == 'send' and execution_id and claim_id
            plan = {'binding': (execution_id, claim_id), 'kwargs': kwargs}
            self.prepared.append(plan)
            return plan

        @single_attempt_transport
        async def send(self, *, prepared_plan=None, execution_id=None, claim_id=None, **kwargs):
            assert prepared_plan is self.prepared[0]
            assert prepared_plan['binding'] == (execution_id, claim_id)
            assert prepared_plan['kwargs'] == kwargs
            self.sent.append(kwargs)
            return SendResult(success=True)

    adapter = Adapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'prepared-plan', [], [])
        await durable.seal(context, 'prepared-plan', 'answer')
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0
        assert db._execute_write(lambda conn: _load(conn, _key('prepared-plan')))['delivery'] == 'delivered'
    assert len(adapter.prepared) == len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_background_claim_refuses_result_changed_during_preflight(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, _load, _save

    class Adapter:
        def __init__(self, db):
            self.db = db
            self.sent = []

        def extract_media(self, text):
            return [], text

        def extract_images(self, text):
            return [], text

        async def _prepare_strict_transport(self, method, **kwargs):
            def change(conn):
                record = _load(conn, _key('changed-during-preflight'))
                record['payload']['text'] = 'changed after preparation'
                _save(conn, _key('changed-during-preflight'), record)
            self.db._execute_write(change)

        @single_attempt_transport
        async def send(self, **kwargs):
            self.sent.append(kwargs)
            return SendResult(success=True)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        adapter = Adapter(db)
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'changed-during-preflight', [], [])
        await durable.seal(context, 'changed-during-preflight', 'original answer')
        assert await durable.recover(runner, db) == 0
        assert adapter.sent == []
        record = db._execute_write(lambda conn: _load(conn, _key('changed-during-preflight')))
        assert record['delivery'] == 'pending'


@pytest.mark.asyncio
async def test_background_claim_refuses_boolean_to_integer_drift(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, _load, _save

    class Adapter:
        def __init__(self, db):
            self.db = db
            self.sent = []

        def extract_media(self, text):
            return [], text

        def extract_images(self, text):
            return [], text

        async def _prepare_strict_transport(self, method, **kwargs):
            def change(conn):
                record = _load(conn, _key('changed-type'))
                record['payload']['metadata']['flag'] = 1
                _save(conn, _key('changed-type'), record)
            self.db._execute_write(change)

        @single_attempt_transport
        async def send(self, **kwargs):
            self.sent.append(kwargs)
            return SendResult(success=True)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        adapter = Adapter(db)
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'changed-type', [], [])
        await durable.seal(context, 'changed-type', 'answer', metadata={'flag':True})
        assert await durable.recover(runner, db) == 0
        assert adapter.sent == []
        record = db._execute_write(lambda conn: _load(conn, _key('changed-type')))
        assert record['delivery'] == 'pending' and type(record['payload']['metadata']['flag']) is int


@pytest.mark.asyncio
async def test_background_ack_does_not_commit_after_payload_changes(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, _load, _save

    class Adapter:
        def __init__(self, db):
            self.db = db
            self.sent = []

        def extract_media(self, text):
            return [], text

        def extract_images(self, text):
            return [], text

        @single_attempt_transport
        async def send(self, **kwargs):
            self.sent.append(kwargs)
            def change(conn):
                record = _load(conn, _key('drift-after-send'))
                record['payload']['text'] = 'altered after claim'
                _save(conn, _key('drift-after-send'), record)
            self.db._execute_write(change)
            return SendResult(success=True)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        adapter = Adapter(db)
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'drift-after-send', [], [])
        await durable.seal(context, 'drift-after-send', 'original')
        assert await durable.recover(runner, db) == 0
        record = db._execute_write(lambda conn: _load(conn, _key('drift-after-send')))
        assert len(adapter.sent) == 1
        assert record['payload']['text'] == 'altered after claim'
        assert record['delivery'] != 'delivered'
        assert await durable.recover(runner, db) == 0
        assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_background_stops_bundle_when_claim_changes_between_sends(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, _load, _save

    class Adapter:
        def __init__(self, db):
            self.db = db
            self.sent = []

        def extract_media(self, text):
            return [], text

        def extract_images(self, text):
            return [('https://example.invalid/image.png', '')], text

        @single_attempt_transport
        async def send(self, **kwargs):
            self.sent.append('text')
            def change(conn):
                record = _load(conn, _key('bundle-drift'))
                record['payload']['text'] = 'changed after first send'
                _save(conn, _key('bundle-drift'), record)
            self.db._execute_write(change)
            return SendResult(success=True)

        @single_attempt_transport
        async def send_image(self, **kwargs):
            self.sent.append('image')
            return SendResult(success=True)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        adapter = Adapter(db)
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
        context = await durable.prepare(runner, 'prompt', source, 'bundle-drift', [], [])
        await durable.seal(context, 'bundle-drift', 'original')
        assert await durable.recover(runner, db) == 0
        assert adapter.sent == ['text']
        record = db._execute_write(lambda conn: _load(conn, _key('bundle-drift')))
        assert record['delivery'] == 'unknown'
        assert await durable.recover(runner, db) == 0
        assert adapter.sent == ['text']