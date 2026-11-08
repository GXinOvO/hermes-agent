import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.strict_transport import strict_call, strict_transport_scope, supports_strict_transport
from tests.gateway.yuanbao_external_fixture import connected_adapter, loaded_yuanbao, new_adapter


@pytest.mark.asyncio
async def test_unload_restores_owned_methods_without_overwriting_another_wrapper(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch) as (manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        sender = adapter._outbound.sender
        assert supports_strict_transport(adapter)
        replacement = AsyncMock()
        adapter.send_image = replacement
        manager.unload('strict-transport-compat')
        assert not supports_strict_transport(adapter)
        assert adapter.send_image is replacement
        assert adapter.send.__func__ is module.YuanbaoAdapter.send
        assert sender.send_text_chunk.__func__ is module.MessageSender.send_text_chunk
        assert sender._dispatch_encoded is module.MessageSender._dispatch_encoded


@pytest.mark.asyncio
async def test_unload_during_strict_send_keeps_single_attempt_until_it_settles(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch) as (manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        adapter._connection._ws = object()
        sender = adapter._outbound.sender
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def lose_ack(*args, **kwargs):
            calls.append(args)
            entered.set()
            await release.wait()
            raise TimeoutError('lost ACK')

        sender._send_msg_body = lose_ack
        task = asyncio.create_task(strict_call(adapter, chat_id='direct:inert', content='sealed'))
        await asyncio.wait_for(entered.wait(), 2)
        manager.unload('strict-transport-compat')
        assert not supports_strict_transport(adapter)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert not result.success and len(calls) == 1
        assert adapter.send.__func__ is module.YuanbaoAdapter.send
        assert sender.send_text_chunk.__func__ is module.MessageSender.send_text_chunk


@pytest.mark.asyncio
@pytest.mark.parametrize('unload', [False, True])
async def test_strict_prepare_rechecks_capability_before_any_send(tmp_path, monkeypatch, unload):
    with loaded_yuanbao(tmp_path, monkeypatch) as (manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        adapter._connection._ws = object()
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(self, method, **kwargs):
            entered.set()
            await release.wait()

        monkeypatch.setattr(module.YuanbaoAdapter, '_prepare_strict_transport', prepare, raising=False)
        send = AsyncMock(side_effect=TimeoutError('lost ACK'))
        adapter._outbound.sender._send_msg_body = send
        task = asyncio.create_task(strict_call(adapter, chat_id='direct:inert', content='sealed'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if unload:
                manager.unload('strict-transport-compat')
            release.set()
            with patch('asyncio.sleep', new=AsyncMock()):
                if unload:
                    with pytest.raises(RuntimeError, match='strict_transport_unverified'):
                        await asyncio.wait_for(task, 2)
                    send.assert_not_awaited()
                else:
                    assert not (await asyncio.wait_for(task, 2)).success
                    assert send.await_count == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unload_after_final_validation_cannot_swap_in_an_unverified_sender(tmp_path, monkeypatch):
    from gateway import strict_transport as transport

    with loaded_yuanbao(tmp_path, monkeypatch) as (manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        adapter._connection._ws = object()
        require = transport.require_strict_transport
        checks = []

        def unload_after_validation(target, method='send'):
            result = require(target, method)
            checks.append(method)
            if len(checks) == 3:
                manager.unload('strict-transport-compat')
            return result

        monkeypatch.setattr(transport, 'require_strict_transport', unload_after_validation)
        send = AsyncMock(side_effect=TimeoutError('lost ACK'))
        adapter._outbound.sender._send_msg_body = send
        with patch('asyncio.sleep', new=AsyncMock()):
            with pytest.raises(RuntimeError, match='unloaded|strict_transport_unverified'):
                await strict_call(adapter, chat_id='direct:inert', content='sealed')
        assert len(checks) == 3
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_discovery_and_force_reload_install_compat(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch, discover=True) as (manager, module, entry, _directory):
        adapter = await connected_adapter(module)
        assert supports_strict_transport(adapter)
        assert entry.adapter_factory is module.YuanbaoAdapter
        manager.discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        import sys

        new_entry = platform_registry.get('yuanbao')
        assert new_entry is not None
        new_module = sys.modules[new_entry.adapter_factory.__module__]
        fresh = await connected_adapter(new_module)
        assert fresh is not adapter and supports_strict_transport(fresh)


@pytest.mark.asyncio
async def test_official_plugin_has_no_strict_capability_before_compat(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch, compat=False) as (_manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        adapter._connection.send_biz_request = AsyncMock()
        for method in ('send', 'send_image', 'send_image_file', 'send_document'):
            assert not supports_strict_transport(adapter, method)
        with pytest.raises(RuntimeError, match='strict_transport_unverified'):
            await strict_call(adapter, chat_id='direct:inert', content='sealed')
        adapter._connection.send_biz_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_plugin_handler_installs_only_audited_methods(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch) as (_manager, module, entry, _directory):
        adapter = new_adapter()
        assert entry.adapter_factory is module.YuanbaoAdapter
        assert not supports_strict_transport(adapter)
        sender = adapter._outbound.sender
        adapter._connection.open = AsyncMock(return_value=True)
        assert await adapter.connect()
        assert adapter._outbound.sender is sender
        for method in ('send', 'send_image', 'send_image_file', 'send_document'):
            assert supports_strict_transport(adapter, method)
        for method in ('send_voice', 'send_video', 'send_multiple_images'):
            assert not supports_strict_transport(adapter, method)
        method = adapter.send
        adapter.rewire_plugin_handlers()
        assert adapter.send is method
        adapter._connection.send_biz_request = AsyncMock(return_value={
            'head': {'cmd_type': module.CMD_TYPE['Response'], 'status': 0, 'msg_id': 'r1'}, 'data': b''})
        dispatch = sender._dispatch_encoded
        with strict_transport_scope():
            assert (await dispatch(adapter, b'inert', 'r1'))['success']
        assert adapter._connection.send_biz_request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['head', 'status', 'id', 'push', 'shape', 'reject', 'business', 'malformed'])
async def test_strict_ack_rejects_bad_envelope_and_business_payload(tmp_path, monkeypatch, fault):
    with loaded_yuanbao(tmp_path, monkeypatch) as (_manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        head = {'cmd_type': module.CMD_TYPE['Response'], 'status': 0, 'msg_id': 'r1'}
        response = {
            'head': {'data': b''},
            'status': {'head': {key: value for key, value in head.items() if key != 'status'}},
            'id': {'head': dict(head, msg_id='other')},
            'push': {'head': dict(head, cmd_type=module.CMD_TYPE['Push'])},
            'shape': None,
            'reject': {'head': dict(head, status=500)},
            'business': {'head': head, 'data': b'\x08\xe7\x07'},
            'malformed': {'head': head, 'data': b'\xff'},
        }[fault]
        adapter._connection.send_biz_request = AsyncMock(return_value=response)
        with strict_transport_scope():
            assert not (await adapter._outbound.sender._dispatch_encoded(adapter, b'inert', 'r1'))['success']
        assert adapter._connection.send_biz_request.await_count == 1


@pytest.mark.asyncio
async def test_strict_and_ordinary_retry_scopes_do_not_leak(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch) as (_manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        sender = adapter._outbound.sender
        sender._send_msg_body = AsyncMock(side_effect=TimeoutError('lost ACK'))

        async def strict():
            with strict_transport_scope():
                return await sender.send_text_chunk('direct:strict', 'sealed')

        with patch('asyncio.sleep', new=AsyncMock()):
            results = await asyncio.gather(strict(), sender.send_text_chunk('direct:human', 'manual'))
        assert all(not result.success for result in results)
        assert [args.args[0] for args in sender._send_msg_body.await_args_list].count('direct:strict') == 1
        assert [args.args[0] for args in sender._send_msg_body.await_args_list].count('direct:human') == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('filename', ['adapter.py', 'send_ack.py', 'yuanbao_media.py', 'yuanbao_proto.py'])
async def test_unknown_implementation_stays_unmarked_and_preserves_ordinary_send(tmp_path, monkeypatch, filename):
    with loaded_yuanbao(tmp_path, monkeypatch) as (_manager, module, _entry, directory):
        path = directory / filename
        path.write_bytes(path.read_bytes() + b'\n')
        adapter = await connected_adapter(module)
        assert not supports_strict_transport(adapter)
        adapter._connection.send_biz_request = AsyncMock(return_value={})
        adapter._connection._ws = object()
        assert (await adapter.send('direct:inert', 'manual')).success
        assert adapter._connection.send_biz_request.await_count == 1


@pytest.mark.asyncio
async def test_group_mentions_still_refresh_the_official_member_cache(tmp_path, monkeypatch):
    with loaded_yuanbao(tmp_path, monkeypatch) as (_manager, module, _entry, _directory):
        adapter = await connected_adapter(module)
        adapter.get_group_member_list = AsyncMock(return_value=[])
        sender = adapter._outbound.sender
        sender._send_msg_body = AsyncMock(return_value={'success': True})
        with strict_transport_scope():
            assert (await sender.send_text_chunk('group:inert', '@Alice hello')).success
        adapter.get_group_member_list.assert_awaited_once_with('inert')


@pytest.mark.asyncio
async def test_each_profile_and_reload_gets_its_own_compat_instance(tmp_path, monkeypatch):
    first_home, second_home = tmp_path / 'first', tmp_path / 'second'
    with loaded_yuanbao(tmp_path, monkeypatch, home=first_home) as (_manager, module_a, _entry, _directory):
        first = await connected_adapter(module_a)
        sender_a = first._outbound.sender
        module_a.YuanbaoAdapter.set_active(first)
        assert module_a.YuanbaoAdapter.get_active() is first
        with loaded_yuanbao(tmp_path, monkeypatch, home=second_home) as (_other, module_b, _entry, _directory):
            second = await connected_adapter(module_b)
            assert module_b is not module_a
            assert second._outbound.sender is not sender_a
            assert supports_strict_transport(second)
            module_b.YuanbaoAdapter.set_active(second)
            assert module_b.YuanbaoAdapter.get_active() is second
        assert module_a.YuanbaoAdapter.get_active() is first
        replacement = await connected_adapter(module_a)
        assert replacement is not first and replacement.send is not first.send
        assert supports_strict_transport(replacement)
