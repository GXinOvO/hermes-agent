import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize('header', [[], {}, None, 0, False])
async def test_bad_header_seal_does_not_block_saved_siblings(tmp_path, header):
    from gateway import background_messaging_results as durable
    from hermes_state_background import PREFIX, claim_delivery

    send = AsyncMock(return_value=SendResult(success=True))
    adapter = SimpleNamespace(send=single_attempt_transport(send),
        extract_media=lambda text:([], text), extract_images=lambda text:([], text))
    with SessionDB(tmp_path / 'state.db') as db:
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:adapter)
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='original')
        for task in ['bad', 'good', 'unknown']:
            context = await durable.prepare(runner, 'read only', source, task, [], [])
            await durable.seal(context, task, 'saved', 'done: ', {})
        assert claim_delivery(db, 'unknown')
        await durable.finish((db, {}, False), 'unknown', 'unknown')
        def damage(conn):
            rows = conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall()
            for key, raw in rows:
                record = json.loads(raw)
                if record['task_id'] == 'bad':
                    record['payload']['header'] = header
                    damaged = json.dumps(record)
                    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (damaged, key))
                    return key, damaged
            raise AssertionError('actual sealed result not found')
        key, raw = db._execute_write(damage)
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0
        send.assert_awaited_once_with(chat_id='original', content='done: saved', metadata={})
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0] == raw
            records = [json.loads(v) for v, in conn.execute('SELECT value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',))]
        by_task = {record['task_id']: record for record in records}
        assert by_task['bad']['delivery'] == 'pending'
        assert by_task['good']['delivery'] == 'delivered'
        assert by_task['unknown']['delivery'] == 'unknown'
        with pytest.raises((TypeError, ValueError)):
            await durable.prepare(runner, 'read only', source, 'bad', [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('metadata', []), ('metadata', False), ('source', []),
    ('source', {}), ('source', {'platform':'not-a-platform', 'chat_id':'original'}),
    ('source', {'platform':'telegram', 'chat_id':[]})])
async def test_bad_messaging_context_is_retained_before_claim(tmp_path, field, value):
    from gateway import background_messaging_results as durable
    from hermes_state_background import PREFIX, _encoded

    send = AsyncMock(return_value=SendResult(success=True))
    adapter = SimpleNamespace(send=single_attempt_transport(send),
        extract_media=lambda text:([], text), extract_images=lambda text:([], text))
    with SessionDB(tmp_path / 'state.db') as db:
        runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:adapter)
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='original')
        for task in ['bad', 'good']:
            context = await durable.prepare(runner, 'read only', source, task, [], [])
            await durable.seal(context, task, 'saved', 'done: ', {})
        def damage(conn):
            rows = conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall()
            for key, raw in rows:
                record = json.loads(raw)
                if record['task_id'] != 'bad':
                    continue
                if field == 'source':
                    record['binding']['source'] = value
                    record['binding_sha256'] = hashlib.sha256(_encoded(record['binding']).encode()).hexdigest()
                else:
                    record['payload'][field] = value
                damaged = _encoded(record)
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (damaged, key))
                return key, damaged
            raise AssertionError('actual sealed result not found')
        key, raw = db._execute_write(damage)
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0
        send.assert_awaited_once_with(chat_id='original', content='done: saved', metadata={})
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0] == raw
