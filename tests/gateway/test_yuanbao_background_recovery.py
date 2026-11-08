import json
from types import SimpleNamespace

import pytest

from gateway.session import SessionSource
from hermes_state import SessionDB
from tests.gateway.test_strict_http_yuanbao import yuanbao_peer


@pytest.mark.asyncio
@pytest.mark.parametrize('compat,fault', [(False, None), (True, None), (True, 'missing-first')])
async def test_real_plugin_background_claim_and_restart_never_repeat_send(tmp_path, monkeypatch, compat, fault):
    from gateway import background_messaging_results as durable
    from hermes_state_background import _key, pending_deliveries

    db_path = tmp_path / 'state.db'
    db = SessionDB(db_path)
    try:
        async with yuanbao_peer(monkeypatch, fault=fault, compat=compat) as (adapter, uploads, frames):
            source = SessionSource(platform=adapter.platform, chat_id='direct:inert')
            runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
            context = await durable.prepare(runner, 'saved work', source, 'bg_yuanbao', [], [])
            await durable.seal(context, 'bg_yuanbao', 'sealed answer')
            assert await durable.recover(runner, db) == int(compat and fault is None)
            record = json.loads(db.get_meta(_key('bg_yuanbao')))
            assert record['delivery'] == ('pending' if not compat else 'unknown' if fault else 'delivered')
            def messages():
                return [frame for frame in frames if frame['head']['cmd'] == 'send_c2c_message']
            assert len(messages()) == int(compat) and not uploads
            assert len(pending_deliveries(db)) == int(not compat)
            assert await durable.recover(runner, db) == 0
            db.close()
            db = SessionDB(db_path)
            runner._session_db = db
            assert await durable.recover(runner, db) == 0
            assert len(messages()) == int(compat) and not uploads
            retained = json.loads(db.get_meta(_key('bg_yuanbao')))
            assert retained['delivery'] == record['delivery']
            import asyncio
            import os
            import sys
            from pathlib import Path

            program = """
import asyncio, json, sys
from types import SimpleNamespace
from gateway import background_messaging_results as durable
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB
from pathlib import Path
from hermes_state_background import _key
sent = []
@single_attempt_transport
async def send(**kwargs):
    sent.append(kwargs)
    return SendResult(success=True)
adapter = SimpleNamespace(send=send, extract_media=lambda text:([], text), extract_images=lambda text:([], text))
db = SessionDB(Path(sys.argv[1]))
try:
    runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
    count = asyncio.run(durable.recover(runner, db))
    print(json.dumps({'sent':len(sent), 'count':count, 'delivery':json.loads(db.get_meta(_key('bg_yuanbao')))['delivery']}))
finally:
    db.close()
"""
            if compat:
                db.close()
                repo = Path(__file__).resolve().parents[2]
                env = dict(os.environ, PYTHONPATH=str(repo))
                child = await asyncio.create_subprocess_exec(sys.executable, '-c', program, str(db_path),
                    cwd=repo, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
                assert child.returncode == 0, stderr.decode()
                assert json.loads(stdout) == {'sent': 0, 'count': 0, 'delivery': record['delivery']}
    finally:
        db.close()
