import asyncio
import copy
import os

import pytest

from gateway.session_controls import AuthorityConnection
from tests.cron import test_sealed_bot_consumer as fixtures
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


def connection(bot, *, native=True, operator=True):
    return AuthorityConnection(bot.authority, lambda *args: None, {
        'user_id': f'uid:{os.getuid()}', 'provider': 'local', 'profile_id': bot.authority.profile_id,
        'capabilities': ['session:create', 'session:read', 'session:submit'],
        'instance_id': bot.authority.instance_id, 'native_bootstrap': native}, operator=operator)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['existing', 'missing', 'empty', 'deleted', 'displaced'])
async def test_native_prepare_creates_no_admission_and_freezes_exact_target(sealed_bot, monkeypatch, case):
    bot = sealed_bot
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda *args, **kwargs: {
        'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})
    await asyncio.to_thread(fixtures._produce, bot)
    params = {**fixtures._params(bot), 'create_missing': True}
    if case in {'missing', 'empty'}:
        assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
        if case == 'empty':
            params['session_id'] = ''
            params['sealed_cron_result_v1'].update(target_session_id='', target_live_session_id='')
    elif case == 'deleted':
        assert bot.authority.db.delete_session(bot.chat)
    elif case == 'displaced':
        assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
        bot.authority.db.create_session('api_displaced', source='api', user_id=bot.connection.actor.subject)
        assert bot.authority.db.set_session_title('api_displaced', 'Bot Chat')
    conn = connection(bot)
    response = await conn.dispatch({'id': 1, 'method': 'bot_relay.prepare', 'params': params})
    assert 'result' in response, response
    owner = response['result']
    assert owner['profile_home'] == str(bot.home)
    assert owner['lease_id'] == bot.authority.instance_id
    assert owner['authority_epoch'] == bot.authority.epoch
    assert owner['session_id'] == owner['live_session_id']
    assert owner['principal_id'] == bot.connection.actor.subject
    assert bool(owner['session_id']) and (owner['session_id'] == bot.chat) == (case == 'existing')
    assert not _admissions(bot)
    again = await conn.dispatch({'id': 2, 'method': 'bot_relay.prepare', 'params': params})
    assert again == {**response, 'id': 2}
    assert not list((bot.home / 'runtime' / 'bot_live_delivery').glob('*.json'))
    from cron.sealed_bot_result import producer_reference
    job, row = fixtures._queued_job(bot)
    params.pop('create_missing')
    params['session_id'] = owner['session_id']
    params['sealed_cron_result_v1'] = producer_reference(job, row['content'], bot.home, owner)
    admitted = await conn.dispatch({'id': 3, 'method': 'bot_relay.deliver', 'params': params})
    assert 'result' in admitted, admitted
    assert admitted['result']['session_id'] == owner['session_id']
    await _settle_quietly(bot)
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['remote', 'nonoperator', 'foreign_profile', 'forged', 'unsealed'])
async def test_prepare_refuses_before_creating_chat(sealed_bot, monkeypatch, case):
    bot = sealed_bot
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda *args, **kwargs: {
        'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})
    await asyncio.to_thread(fixtures._produce, bot)
    params = {**copy.deepcopy(fixtures._params(bot)), 'create_missing': True}
    assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
    if case == 'foreign_profile':
        params['profile'] = 'foreign'
    elif case == 'forged':
        params['sealed_cron_result_v1']['seal'] = '0' * 64
    elif case == 'unsealed':
        params.pop('sealed_cron_result_v1')
    conn = connection(bot, native=case != 'remote', operator=case != 'nonoperator')
    response = await conn.dispatch({'id': 1, 'method': 'bot_relay.prepare', 'params': params})
    assert response['error']['data']['reason'] in {'permission_denied', 'profile_mismatch', 'admission_conflict'}, response
    assert bot.authority.db.get_session_by_title('Bot Chat') is None
    assert not _admissions(bot)


@pytest.mark.asyncio
async def test_saved_prepare_refuses_retarget_without_creating_chat(sealed_bot):
    bot = sealed_bot
    await asyncio.to_thread(fixtures._produce, bot)
    params = fixtures._params(bot)
    assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
    response = await connection(bot).dispatch({'id': 1, 'method': 'bot_relay.prepare', 'params': params})
    assert response['error']['data']['reason'] == 'admission_conflict', response
    assert bot.authority.db.get_session_by_title('Bot Chat') is None
    assert not _admissions(bot)


@pytest.mark.asyncio
async def test_prepared_delivery_target_drift_refuses_without_creating_chat(sealed_bot, monkeypatch):
    bot = sealed_bot
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda *args, **kwargs: {
        'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})
    await asyncio.to_thread(fixtures._produce, bot)
    params = fixtures._params(bot)
    conn = connection(bot)
    prepared = await conn.dispatch({'id': 1, 'method': 'bot_relay.prepare', 'params': params})
    assert 'result' in prepared, prepared
    assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
    response = await conn.dispatch({'id': 2, 'method': 'bot_relay.deliver', 'params': params})
    assert response['error']['data']['reason'] == 'admission_conflict', response
    assert bot.authority.db.get_session_by_title('Bot Chat') is None
    assert not _admissions(bot)
