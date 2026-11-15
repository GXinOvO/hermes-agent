"""``session.resume`` with an owner witness: exact-source resume refuses a moved store (NAV-EXACT)."""
import asyncio
from types import SimpleNamespace

import pytest


def _owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    import hermes_state_runtime as rt
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {})
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _handle_message=lambda event: 'unused',
                             _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db,
                                 epoch=rt.begin_runtime_epoch(db, instance_id='owner'))
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, owner.actor, dict(request_id='m', source='gui', cwd=str(tmp_path),
                                                            model='frozen', toolsets=[]))
    return db, authority, owner, ref


def _witness(authority, session_id, **overrides):
    witness = {'instance_id': authority.instance_id, 'profile_id': authority.profile_id,
               'authority_epoch': authority.epoch, 'store_instance_id': authority.db.get_meta('store_instance_id')}
    witness.update(overrides)
    return {'session_id': session_id, 'expected_owner': witness}


async def _resume(owner, params):
    return await owner.dispatch({'jsonrpc': '2.0', 'id': 7, 'method': 'session.resume', 'params': params})


@pytest.mark.asyncio
async def test_plain_resume_still_resumes_without_a_witness(tmp_path, monkeypatch):
    _db, _authority, owner, ref = _owner(tmp_path, monkeypatch)

    reply = await _resume(owner, {'session_id': ref.session_id})

    assert 'error' not in reply, reply
    assert reply['result']['session_id'] == ref.session_id


@pytest.mark.asyncio
async def test_a_matching_witness_resumes_the_owning_store(tmp_path, monkeypatch):
    db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    store_instance_id = db.get_meta('store_instance_id')

    reply = await _resume(owner, _witness(authority, ref.session_id, store_instance_id=store_instance_id))

    assert 'error' not in reply, reply
    assert reply['result']['session_id'] == ref.session_id
    assert owner.subscriptions.get(ref.session_id)
    assert reply['result']['owner'] == _witness(authority, ref.session_id,
        store_instance_id=store_instance_id)['expected_owner']


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [
    pytest.param({'instance_id': 'replacement'}, id='instance'),
    pytest.param({'authority_epoch': 99}, id='epoch'),
    pytest.param({'profile_id': 'other'}, id='profile'),
    pytest.param({'store_instance_id': 'a-copied-store'}, id='store-copy'),
])
async def test_a_moved_owner_is_refused_and_leaves_no_subscription(tmp_path, monkeypatch, overrides):
    _db, authority, owner, ref = _owner(tmp_path, monkeypatch)

    reply = await _resume(owner, _witness(authority, ref.session_id, **overrides))

    assert reply['error']['data']['reason'] == 'session_owner_changed', reply
    assert ref.session_id not in owner.subscriptions
    assert ref.session_id not in owner.reconnects


@pytest.mark.asyncio
@pytest.mark.parametrize('witness', [
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned'}, id='missing-epoch'),
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned', 'authority_epoch': 1, 'extra': 'x'},
                 id='unknown-key'),
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned', 'authority_epoch': '1'}, id='string-epoch'),
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned', 'authority_epoch': 0}, id='zero-epoch'),
    pytest.param({'instance_id': ' owner', 'profile_id': 'owned', 'authority_epoch': 1}, id='padded-instance'),
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned', 'authority_epoch': 1, 'store_instance_id': None},
                 id='null-store'),
    pytest.param({'instance_id': 'owner', 'profile_id': 'owned', 'authority_epoch': 1, 'store_instance_id': ''},
                 id='empty-store'),
    pytest.param('owner', id='not-an-object'),
])
async def test_a_malformed_witness_never_reaches_the_handler(tmp_path, monkeypatch, witness):
    db, _authority, owner, ref = _owner(tmp_path, monkeypatch)
    if isinstance(witness, dict):
        witness = {'store_instance_id': db.get_meta('store_instance_id'), **witness}

    reply = await _resume(owner, {'session_id': ref.session_id, 'expected_owner': witness})

    data = reply['error']['data']
    assert data['reason'] == 'invalid_params', reply
    named = [data.get('field'), *(data.get('fields') or [])]
    assert any(isinstance(name, str) and name.startswith('expected_owner') for name in named), reply
    assert not owner.subscriptions.get(ref.session_id)


@pytest.mark.asyncio
async def test_an_incomplete_witness_never_reaches_the_handler(tmp_path, monkeypatch):
    _db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    params = _witness(authority, ref.session_id)
    del params['expected_owner']['store_instance_id']

    reply = await _resume(owner, params)

    assert reply['error']['data']['reason'] == 'invalid_params', reply
    assert not owner.subscriptions.get(ref.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [pytest.param({'title': 'x'}, id='title'), pytest.param({'latest': True}, id='latest')])
async def test_a_witness_rides_an_exact_resume_only(tmp_path, monkeypatch, extra):
    _db, authority, owner, ref = _owner(tmp_path, monkeypatch)

    reply = await _resume(owner, {**_witness(authority, ref.session_id), **extra})

    assert reply['error']['data']['reason'] == 'invalid_params', reply
    assert not owner.subscriptions.get(ref.session_id)


@pytest.mark.asyncio
async def test_a_store_replaced_during_the_attach_is_refused_after_it(tmp_path, monkeypatch):
    db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    from gateway.session_authority import SessionAuthority
    import hermes_state_runtime as rt
    replacement = SessionAuthority(authority.runner, profile_id='owned', instance_id='replacement', db=db,
                                   epoch=rt.begin_runtime_epoch(db, instance_id='replacement'))
    original_attach = authority.attach

    async def attach_then_move(actor, target, **kwargs):
        snapshot = await original_attach(actor, target, **kwargs)
        owner.authority = replacement
        return snapshot

    monkeypatch.setattr(authority, 'attach', attach_then_move)

    reply = await _resume(owner, _witness(authority, ref.session_id))

    assert reply['error']['data']['reason'] == 'session_owner_changed', reply
    assert ref.session_id not in owner.subscriptions
    assert ref.session_id not in owner.reconnects
    assert not authority.sessions[ref.session_id].subscribers


@pytest.mark.asyncio
async def test_the_runtime_describe_answers_the_store_the_witness_names(tmp_path, monkeypatch):
    db, _authority, owner, _ref = _owner(tmp_path, monkeypatch)

    reply = await owner.dispatch({'jsonrpc': '2.0', 'id': 8, 'method': 'runtime.describe', 'params': {}})

    assert reply['result']['store_instance_id'] == db.get_meta('store_instance_id')


@pytest.mark.asyncio
async def test_refused_reopen_preserves_an_existing_subscription(tmp_path, monkeypatch):
    db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    first = await _resume(owner, _witness(authority, ref.session_id,
        store_instance_id=db.get_meta('store_instance_id')))
    subscription = first['result']['subscription_id']

    reply = await _resume(owner, _witness(authority, ref.session_id, instance_id='replacement'))

    assert reply['error']['data']['reason'] == 'session_owner_changed'
    assert owner.subscriptions[ref.session_id] == subscription
    assert authority.sessions[ref.session_id].subscribers[subscription] == owner.actor
