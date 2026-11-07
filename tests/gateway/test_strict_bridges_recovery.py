"""Common final dispatcher plus fresh-process consume-only transport receipts."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.final_delivery import send_attachments, send_text
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.gateway.test_strict_bridges_http import connected, http_peer, target
from tests.gateway.test_strict_bridges_local import simplex_peer


@asynccontextmanager
async def transport(kind, fault, monkeypatch):
    if kind == 'simplex':
        async with simplex_peer(fault) as (wire, adapter):
            yield wire, adapter, '123'
    else:
        async with http_peer(fault) as wire, connected(kind, wire, monkeypatch) as adapter:
            yield wire, adapter, target(kind)


async def sealed(home, kind, adapter, chat_id, *, media=False):
    from gateway import pending_final as pf
    db = SessionDB(home / 'state.db')
    db.create_session('sid', source=kind)
    epoch = begin_runtime_epoch(db, instance_id='owner')
    route = f'agent:main:{kind}:dm:{chat_id}'
    source = SessionSource(platform=Platform(kind), chat_id=chat_id, chat_type='dm')
    envelope = {'route':route, 'source':{**source.to_dict(), 'is_bot':False}, 'event':{}, 'timestamp':'1970-01-01T00:00:00+00:00',
                'automation':{'owner':'sid', 'identity':'sealed-work', 'result_recovery_v1':'consume_only'}}
    payload = {'text':'completed work', 'native_text_v1':envelope}
    admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                        request_id='sealed-work', payload=payload)
    row = claim_session_input(db, epoch=epoch, session_id='sid')
    event = MessageEvent(text='completed work', source=source, internal=True, message_id='sealed-work')
    authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source:adapter))
    if media:
        from PIL import Image
        path = home / 'asset.png'
        Image.new('RGB', (4, 4), (1, 2, 3)).save(path)
        if kind == 'line':
            adapter.public_base_url = 'https://local.invalid'
        response = 'MEDIA:' + str(path)
    else:
        response = 'sealed answer'
    async with automation_delivery_scope(authority, row, event):
        prepared = await pf.prepare(authority, row, event, route, response)
    finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
                  result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':prepared}, 'usage':{}})
    return db, row, event, prepared, response


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'matrix', 'qqbot', 'simplex'])
@pytest.mark.parametrize('fault', [None, 'lost'])
@pytest.mark.parametrize('media', [False, True])
async def test_dispatcher_seals_and_never_replays_same_final(kind, fault, media, tmp_path, monkeypatch):
    from gateway import pending_final as pf
    async with transport(kind, fault, monkeypatch) as (wire, adapter, chat):
        db, row, event, descriptor, response = await sealed(tmp_path, kind, adapter, chat, media=media)
        try:
            if media:
                extracted = await adapter._extract_response_content(response, event, descriptor['binding']['route'],
                                                                     is_ephemeral_response=False)
                outcomes = []
                for _ in range(2):
                    await send_attachments(adapter, event, extracted, descriptor['metadata'],
                                           anything_sent=False, record_delivery=outcomes.append)
            else:
                outcomes = [await send_text(adapter, event, descriptor['binding']['route'], response,
                                            reply_to=descriptor['reply_to'], metadata=descriptor['metadata']) for _ in range(2)]
            assert [r.success for r in outcomes] == [fault is None, fault is None]
            key = 'attachments' if media else 'text'
            part = pf.pending(db)[0]['parts'][key]
            assert (part['state'], part['attempts']) == ('delivered' if fault is None else 'unknown', 1)
            expected = {'matrix':2, 'qqbot':5}.get(kind, 1) if media and not fault else 1
            assert len(wire.events) == expected
            assert pf.claim(db, row['admission_id'], key, part['content'], recovering=True)[1] is False
        finally:
            db.close()


_PEER = r'''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from gateway import pending_final as pf
from gateway.config import PlatformConfig
from hermes_state import SessionDB
from tests.gateway.test_strict_bridges_recovery import sealed

home, kind, mode, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
async def main():
    if kind == 'line':
        from plugins.platforms.line import adapter as m
        m.LINE_PUSH_URL = base + '/push'
        adapter = m.LineAdapter(PlatformConfig(enabled=True))
        adapter._client = m._LineClient('local-only', timeout=1)
        chat = 'Upeer'
    elif kind == 'qqbot':
        import time, httpx
        from plugins.platforms.qqbot import adapter as m
        m.API_BASE = base
        from tools import url_safety
        url_safety._global_allow_private_urls = lambda: True
        adapter = m.QQAdapter(PlatformConfig(enabled=True, extra={'app_id':'local-account'}))
        from plugins.platforms.qqbot.routes import ChatRoutes
        ChatRoutes(adapter._app_id).remember('peer', 'c2c')
        adapter._http_client = httpx.AsyncClient(timeout=1, trust_env=False)
        adapter._running = True
        adapter._ws = SimpleNamespace(closed=False)
        adapter._access_token = 'local-only'
        adapter._token_expires_at = time.time() + 3600
        chat = 'peer'
    elif kind == 'matrix':
        from mautrix.client import Client
        from plugins.platforms.matrix import adapter as m
        adapter = m.MatrixAdapter(PlatformConfig(enabled=True))
        adapter._client = Client(base_url=base, mxid='@bot:local', token='local-only',
                                 client_session=m._create_matrix_session(None))
        chat = '!room:local'
    elif kind in {'a2a', 'raft', 'buzz', 'photon', 'irc'}:
        import importlib
        names = {'a2a':'A2AAdapter', 'raft':'RaftAdapter', 'buzz':'BuzzAdapter', 'photon':'PhotonAdapter', 'irc':'IRCAdapter'}
        m = importlib.import_module('plugins.platforms.' + kind + '.adapter')
        adapter = getattr(m, names[kind])(PlatformConfig(enabled=True))
        chat = 'peer'
    else:
        from websockets.asyncio.client import connect
        from plugins.platforms.simplex.adapter import SimplexAdapter
        adapter = SimplexAdapter(PlatformConfig(enabled=True))
        adapter._ws = await connect(base)
        adapter._strict_ack_timeout = 0.2
        async def listen():
            async for raw in adapter._ws:
                await adapter._handle_event(json.loads(raw))
        listener = asyncio.create_task(listen())
        chat = '123'
    try:
        if mode == 'seal':
            db, row, event, descriptor, response = await sealed(home, kind, adapter, chat)
            db.close()
            print(json.dumps({'sealed':True}), flush=True)
            return
        db = SessionDB(home / 'state.db')
        entry = SimpleNamespace(session_id='sid', suspended=False)
        from gateway.session import SessionStore
        from gateway.config import GatewayConfig
        store = SessionStore(sessions_dir=home / 'sessions', config=GatewayConfig())
        value = pf.pending(db)[0]
        entry.session_key = value['binding']['route']
        # Use the real route validator and source authorization; only storage lookup is inert.
        store.lookup_by_session_key = lambda route: entry if route == value['binding']['route'] else None
        authority = SimpleNamespace(db=db, profile_id=str(home), logical_owner=lambda sid:sid)
        from gateway.session_authorities import SessionAuthorities
        registry = SessionAuthorities(home)
        registry.add(home, authority)
        runner = SimpleNamespace(session_authorities=registry, session_authority=authority,
            session_store=store, config=SimpleNamespace(multiplex_profiles=False),
            _adapter_for_source=lambda source:adapter, _delivery_adapter_for=lambda source:adapter)
        authority.runner = runner
        adapter.authorize_source = lambda source: True
        delivered = await pf.recover(runner)
        print(json.dumps({'delivered':delivered, 'part':pf.pending(db)[0]['parts']['text']}), flush=True)
        db.close()
    finally:
        if kind == 'matrix': await adapter._client.api.session.close()
        elif kind == 'qqbot': await adapter._http_client.aclose()
        elif kind == 'simplex':
            await adapter._ws.close()
            await listener
asyncio.run(main())
'''


async def process(home, kind, mode, base):
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home / 'user'))
    env['PYTHONPATH'] = os.pathsep.join([str(Path(__file__).resolve().parents[2]), *sys.path])
    result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', _PEER, str(home), kind, mode, base],
                                     env=env, capture_output=True, text=True, timeout=30, check=False)
    print('PROCESS_RECEIPT ' + json.dumps({'kind':kind, 'mode':mode, 'exit':result.returncode,
          'stdout':result.stdout, 'stderr':result.stderr}))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    receipt['stderr'] = result.stderr
    return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'qqbot', 'matrix', 'simplex'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_fresh_owner_public_recovery_consumes_only_once(kind, fault, tmp_path, monkeypatch):
    async with transport(kind, fault, monkeypatch) as (wire, _adapter, _chat):
        assert (await process(tmp_path, kind, 'seal', wire.base))['sealed'] is True
        first = await process(tmp_path, kind, 'recover', wire.base)
        assert first['delivered'] == int(fault is None), first
        assert (first['part']['state'], first['part']['attempts']) == ('delivered' if fault is None else 'unknown', 1)
        before = list(wire.events)
        second = await process(tmp_path, kind, 'recover', wire.base)
        assert second['delivered'] == 0 and wire.events == before
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'qqbot', 'matrix'])
async def test_fresh_reader_does_not_take_live_sealer(kind, tmp_path, monkeypatch):
    async with http_peer() as wire, connected(kind, wire, monkeypatch) as adapter:
        db, _row, _event, _prepared, _response = await sealed(tmp_path, kind, adapter, target(kind))
        try:
            receipt = await process(tmp_path, kind, 'recover', wire.base)
            assert receipt['delivered'] == 0
            assert receipt['part']['state'] == 'pending' and receipt['part']['attempts'] == 0
            assert not wire.events
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['a2a', 'raft', 'buzz', 'photon', 'irc'])
async def test_fresh_owner_unqualified_target_retains_sealed_result_without_claim(kind, tmp_path, monkeypatch):
    # These fixtures seal the unqualified chat_id 'peer': A2A lacks its exact
    # trusted task binding and Raft lacks an exact channel. Their send method
    # markers do not grant publication for this target. Other bridges remain
    # unverified. Exact Raft/A2A positives have separate native qualifications.
    async with http_peer() as wire:
        assert (await process(tmp_path, kind, 'seal', wire.base))['sealed'] is True
        for _ in range(2):
            receipt = await process(tmp_path, kind, 'recover', wire.base)
            assert receipt['delivered'] == 0
            assert receipt['part']['state'] == 'pending' and receipt['part']['attempts'] == 0
        assert not wire.events
