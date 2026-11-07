"""Preclaim refusal, frozen bytes, and independent per-claim capabilities."""
import asyncio
import json

import pytest

from agent import secret_scope
from gateway.config import PlatformConfig
from plugins.platforms.raft.adapter import RaftAdapter
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['default', 'runtime-session', 'loopback', '#one #two', '#x:BAD', '#'])
async def test_aggregate_or_unqualified_targets_refused_before_claim(target, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        with pytest.raises((RuntimeError, ValueError), match='target|aggregate'):
            await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)), chat=target)
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['{', '[]', '{}', '{"apiKey": true}',
                                '{"apiKey":"x","agentId":"a","serverUrl":"https://u:p@host"}'])
async def test_broken_profiles_cannot_spend_claim(raw, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        path, _ = transport.credential(tmp_path, monkeypatch, wire.url)
        path.write_text(raw)
        with pytest.raises((RuntimeError, ValueError), match='credential'):
            await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
        assert wire.events == []


@pytest.mark.asyncio
async def test_scoped_miss_never_borrows_default_profile(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        multiplex = secret_scope.set_multiplex_context(True)
        scope = secret_scope.set_secret_scope({}, profile_home=str(tmp_path / 'secondary'))
        try:
            with pytest.raises((RuntimeError, ValueError), match='profile'):
                await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
        finally:
            secret_scope.reset_secret_scope(scope)
            secret_scope.reset_multiplex_context(multiplex)
        assert wire.events == []


@pytest.mark.asyncio
async def test_frozen_wire_survives_endpoint_token_config_and_target_drift(tmp_path, monkeypatch):
    async with transport.peer() as original, transport.peer() as foreign:
        path, value = transport.credential(tmp_path, monkeypatch, original.url)
        config = PlatformConfig(enabled=True)
        adapter = RaftAdapter(config)
        plan = await transport.prepare(adapter)
        value.update(apiKey='sk_agent_foreign-inert-only', serverUrl=foreign.url)
        path.write_text(json.dumps(value))
        monkeypatch.setenv('RAFT_PROFILE', 'foreign')
        config.extra['server_url'] = foreign.url
        config.extra['target'] = '#foreign'
        result = await transport.consume(adapter, plan)
        assert result.success
        assert len(original.events) == 1 and foreign.events == []
        assert original.events[0]['authorization'] == 'Bearer sk_agent_inert-test-only'
        assert original.events[0]['body']['target'] == '#loopback'
        from plugins.platforms.raft.strict_delivery import binding
        audit = json.dumps(binding(plan))
        assert 'sk_agent_' not in audit and 'sealed-raft-loopback-result' not in audit


@pytest.mark.asyncio
async def test_two_simultaneous_plans_same_config_do_not_cross(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        first, second = await asyncio.gather(
            transport.prepare(adapter, chat='#first', execution='result-A', claim='result-A:text'),
            transport.prepare(adapter, chat='#second', execution='result-B', claim='result-B:text'))
        with pytest.raises(RuntimeError, match='binding'):
            await transport.consume(adapter, first, chat='#second', execution='result-B', claim='result-B:text')
        assert wire.events == []
        results = await asyncio.gather(
            transport.consume(adapter, second, chat='#second', execution='result-B', claim='result-B:text'),
            transport.consume(adapter, first, chat='#first', execution='result-A', claim='result-A:text'))
        assert all(result.success for result in results)
        assert {e['body']['target'] for e in wire.events} == {'#first', '#second'}
        assert len({e['body']['idempotencyKey'] for e in wire.events}) == 2


@pytest.mark.asyncio
async def test_simultaneous_consumers_submit_only_one_body(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        results = await asyncio.gather(transport.consume(adapter, plan), transport.consume(adapter, plan), return_exceptions=True)
        assert sum(getattr(result, 'success', False) for result in results) == 1
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'target', 'claim', 'execution', 'reply', 'metadata', 'adapter'])
async def test_changed_caller_binding_cannot_mutate_wire(change, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        kwargs = {'body': {'content':'changed'}, 'target': {'chat':'#foreign'}, 'claim': {'claim':'foreign'},
                  'execution': {'execution':'foreign'}, 'reply': {'reply':'root'},
                  'metadata': {'metadata': {'thread_id':'root'}}, 'adapter': {}}[change]
        if change == 'adapter':
            adapter = RaftAdapter(PlatformConfig(enabled=True))
        with pytest.raises(RuntimeError, match='binding|foreign'):
            await transport.consume(adapter, plan, **kwargs)
        assert wire.events == []


@pytest.mark.asyncio
async def test_idempotency_key_is_stable_for_fresh_owner_preparation(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        from plugins.platforms.raft.strict_delivery import binding
        one = await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
        two = await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
        assert binding(one)['idempotency_key'] == binding(two)['idempotency_key']
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'content':'answer\nMEDIA:/inert/image.png'}, {'reply':'root'},
                                    {'metadata': {'thread_id':'root'}}, {'execution':None}, {'claim':None}])
async def test_unsupported_shape_refused_before_claim(kwargs, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        with pytest.raises((RuntimeError, ValueError)):
            await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)), **kwargs)
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['profile_alias', 'directory_alias', 'managed', 'relative', 'nonloopback_http', 'missing'])
async def test_profile_resolution_refuses_ambiguous_or_unsafe_inputs(fault, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        path, value = transport.credential(tmp_path, monkeypatch, wire.url)
        settings = {'profile_alias': ('SLOCK_PROFILE', 'foreign'),
                    'directory_alias': ('SLOCK_PROFILE_DIR', str(tmp_path / 'foreign')),
                    'managed': ('SLOCK_CLI_TRANSPORT_DIR', str(tmp_path / 'managed')),
                    'relative': ('RAFT_PROFILE_DIR', 'relative')}
        if fault in settings:
            monkeypatch.setenv(*settings[fault])
        if fault == 'missing':
            path.unlink()
        if fault == 'nonloopback_http':
            value['serverUrl'] = 'http://192.0.2.1'
            path.write_text(json.dumps(value))
        with pytest.raises((RuntimeError, ValueError)):
            await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('selection', ['raft_home', 'slock_home', 'new_home', 'legacy_home', 'scoped_own'])
async def test_official_profile_location_uses_inert_owning_home(selection, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        _path, value = transport.credential(tmp_path, monkeypatch, wire.url)
        monkeypatch.delenv('RAFT_PROFILE_DIR')
        monkeypatch.setenv('HERMES_REAL_HOME', str(tmp_path))
        if selection in {'raft_home', 'slock_home'}:
            root = tmp_path / selection
            monkeypatch.setenv('RAFT_HOME' if selection == 'raft_home' else 'SLOCK_HOME', str(root))
        else:
            root = tmp_path / ('.slock' if selection == 'legacy_home' else '.raft')
        own = root / 'profiles/inert/credential.json'
        own.parent.mkdir(parents=True)
        own.write_text(json.dumps(value))
        multiplex = scope = None
        if selection == 'scoped_own':
            monkeypatch.setenv('RAFT_PROFILE_DIR', str(tmp_path / 'default-foreign'))
            multiplex = secret_scope.set_multiplex_context(True)
            scope = secret_scope.set_secret_scope({'RAFT_PROFILE':'inert', 'RAFT_HOME':str(root)},
                                                 profile_home=str(tmp_path / 'secondary'))
        try:
            plan = await transport.prepare(RaftAdapter(PlatformConfig(enabled=True)))
            from plugins.platforms.raft.strict_delivery import binding
            assert binding(plan)['credential_path'] == str(own.resolve())
        finally:
            if scope is not None:
                secret_scope.reset_secret_scope(scope)
                secret_scope.reset_multiplex_context(multiplex)
        assert wire.events == []
