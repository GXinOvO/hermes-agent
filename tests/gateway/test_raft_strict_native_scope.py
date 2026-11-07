import json

import pytest

from agent import secret_scope
from gateway.config import PlatformConfig
from plugins.platforms.raft.adapter import RaftAdapter
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_scoped_profile_without_own_root_never_reads_ambient_credentials(
        legacy, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        _, value = transport.credential(tmp_path, monkeypatch, wire.url)
        default_home = tmp_path / 'default-owner'
        credential = default_home / ('.slock' if legacy else '.raft') / 'profiles/inert/credential.json'
        credential.parent.mkdir(parents=True)
        credential.write_text(json.dumps(value))
        monkeypatch.setenv('HERMES_REAL_HOME', str(default_home))
        monkeypatch.delenv('RAFT_PROFILE_DIR')
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter, execution='default', claim='default:text')
        assert (await transport.consume(adapter, plan, execution='default', claim='default:text')).success
        assert len(wire.events) == 1
        multiplex = secret_scope.set_multiplex_context(True)
        scope = secret_scope.set_secret_scope({'RAFT_PROFILE': 'inert'},
                                              profile_home=str(tmp_path / 'secondary'))
        reads = []
        original_read = type(credential).read_bytes

        def observe_read(path):
            if path == credential:
                reads.append(str(path))
            return original_read(path)

        monkeypatch.setattr(type(credential), 'read_bytes', observe_read)
        try:
            with pytest.raises((RuntimeError, ValueError), match='profile|scope'):
                await transport.prepare(adapter, execution='secondary', claim='secondary:text')
            assert reads == []
            assert len(wire.events) == 1
        finally:
            secret_scope.reset_secret_scope(scope)
            secret_scope.reset_multiplex_context(multiplex)


@pytest.mark.asyncio
async def test_explicit_own_profile_directory_remains_usable(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        credential, _ = transport.credential(tmp_path, monkeypatch, wire.url)
        multiplex = secret_scope.set_multiplex_context(True)
        scope = secret_scope.set_secret_scope(
            {'RAFT_PROFILE': 'inert', 'RAFT_PROFILE_DIR': str(credential.parent)},
            profile_home=str(tmp_path / 'secondary'))
        try:
            adapter = RaftAdapter(PlatformConfig(enabled=True))
            plan = await transport.prepare(adapter)
            assert (await transport.consume(adapter, plan)).success
            assert len(wire.events) == 1
        finally:
            secret_scope.reset_secret_scope(scope)
            secret_scope.reset_multiplex_context(multiplex)
