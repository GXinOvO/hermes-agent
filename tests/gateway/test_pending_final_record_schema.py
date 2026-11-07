from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from gateway.config import GatewayConfig
from gateway.platforms.base import SendResult
from gateway.session import SessionStore
from gateway.session_authorities import SessionAuthorities
from gateway.strict_transport import single_attempt_transport
from tests.gateway.test_pending_final import Adapter
from tests.gateway.test_strict_bridges_recovery import sealed


class CountingAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.sent = []

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id='ack')


MUTATIONS = [
    ('adapter_profile', 'remove'), ('metadata', 'remove'), ('metadata', []),
    ('reply_to', 'remove'), ('reply_to', {}), ('owner_pid', 'remove'), ('owner_pid', {}),
    ('owner_started_at', 'remove'), ('owner_started_at', []), ('created_at', 'nan'),
    pytest.param('created_at', 10**400, id='created_at-overflow-positive'),
    pytest.param('created_at', -(10**400), id='created_at-overflow-negative'),
    ('binding.route', 'remove'), ('binding.route', []), ('binding.source', 'remove'),
    ('binding.source', []), ('binding.source.platform', 'not-a-platform'),
    ('binding.source.chat_id', 'remove'), ('binding.generation', 'bad'),
    ('binding.version', 2), ('parts.text.attempts', 'bad'),
    ('parts.text.content', 'remove'), ('parts.text.content', {}),
    ('parts.text.payload_hash', 'bad'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('path,value', MUTATIONS)
async def test_complete_but_damaged_record_never_blocks_trusted_sibling(path, value, tmp_path, monkeypatch):
    adapter = CountingAdapter()
    db, row, _event, descriptor, response = await sealed(tmp_path, 'slack', adapter, 'chat')
    try:
        original = pf.pending(db)[0]
        damaged = deepcopy(original)
        damaged['binding']['admission_id'] = '000-bad'
        target = damaged
        keys = path.split('.')
        for key in keys[:-1]:
            target = target[key]
        if value == 'remove':
            target.pop(keys[-1])
        else:
            target[keys[-1]] = float('nan') if value == 'nan' else value
        raw = json.dumps(damaged)
        def arrange(conn):
            conn.execute('DELETE FROM state_meta WHERE key=?', (pf.PREFIX + row['admission_id'],))
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (pf.PREFIX + '000-bad', raw))
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (pf.PREFIX + row['admission_id'], json.dumps(original)))
        db._execute_write(arrange)
        store = SessionStore(sessions_dir=tmp_path / 'sessions', config=GatewayConfig())
        entry = SimpleNamespace(session_id='sid', session_key=descriptor['binding']['route'], suspended=False)
        store.lookup_by_session_key = lambda route:entry if route == entry.session_key else None
        authority = SimpleNamespace(db=db, profile_id=str(tmp_path), logical_owner=lambda sid:sid)
        authorities = SessionAuthorities(tmp_path)
        authorities.add(tmp_path, authority)
        runner = SimpleNamespace(session_authorities=authorities, session_authority=authority, session_store=store,
            config=SimpleNamespace(multiplex_profiles=False), _adapter_for_source=lambda source:adapter,
            _delivery_adapter_for=lambda source:adapter)
        authority.runner = runner
        adapter.authorize_source = lambda source:True
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda pid, birth:False)
        assert await pf.recover(runner) == 1
        assert adapter.sent == [('chat', response)]
        assert await pf.recover(runner) == 0
        assert adapter.sent == [('chat', response)]
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (pf.PREFIX + '000-bad',)).fetchone()[0] == raw
            valid = pf._read(conn, row['admission_id'])
        assert (valid['parts']['text']['state'], valid['parts']['text']['attempts']) == ('delivered', 1)
    finally:
        db.close()
