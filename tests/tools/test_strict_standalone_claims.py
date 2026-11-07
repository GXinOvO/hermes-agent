"""Independent preparations and atomic consumption through real local submissions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import gc
import threading
import weakref

import pytest

from gateway.config import PlatformConfig
from tests.tools import test_strict_standalone_dispatch as fixtures
from tools import send_message_strict as strict


@pytest.mark.asyncio
async def test_independent_concurrent_prepares_keep_their_own_credentials(monkeypatch):
    from tools import send_message_strict_slack as slack
    original = slack.selected_token
    barrier = threading.Barrier(2)
    config = PlatformConfig(token='inert-first')
    first_selected = threading.Event()

    def token(config):
        value = original(config)
        first_selected.set()
        barrier.wait(timeout=10)
        return value

    def prepare(index):
        if index == 1:
            assert first_selected.wait(timeout=10)
            config.token = 'inert-second'
        return strict.prepare('slack', config, 'C555', 'sealed', execution_id='exec', claim_id=str(index))

    monkeypatch.setattr(slack, 'selected_token', token)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='prepare') as pool:
            a = pool.submit(prepare, 0)
            b = pool.submit(prepare, 1)
            plans = await asyncio.to_thread(lambda: [a.result(timeout=15), b.result(timeout=15)])
        results = []
        for index, plan in enumerate(plans):
            results.append(await strict.send('slack', config, 'C555', 'sealed', prepared_plan=plan,
                                             execution_id='exec', claim_id=str(index)))
        assert all(result.get('success') for result in results), results
        assert [event['authorization'] for event in wire.events] == ['Bearer inert-first', 'Bearer inert-second']
        assert not hasattr(config, '_strict_standalone_plans')


@pytest.mark.asyncio
async def test_one_plan_two_simultaneous_senders_submit_once(monkeypatch):
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'discord')
        config = PlatformConfig(token='inert')
        plan = strict.prepare('discord', config, '555', 'sealed', execution_id='exec', claim_id='claim')
        barrier = threading.Barrier(2)

        def send():
            barrier.wait(timeout=10)
            return asyncio.run(strict.send('discord', config, '555', 'sealed', prepared_plan=plan,
                                           execution_id='exec', claim_id='claim'))

        results = await asyncio.gather(asyncio.to_thread(send), asyncio.to_thread(send))
        assert sum(bool(result.get('success')) for result in results) == 1, results
        assert len(wire.events) == 1
        assert next(result for result in results if not result.get('success')).get('ambiguous') is True


@pytest.mark.asyncio
async def test_binding_rejects_wrong_execution_claim_thread_without_consuming(monkeypatch):
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'discord')
        config = PlatformConfig(token='inert')
        plan = strict.prepare('discord', config, '555', 'sealed', thread_id='777', execution_id='exec', claim_id='claim')
        for execution, claim, thread in [('wrong', 'claim', '777'), ('exec', 'wrong', '777'), ('exec', 'claim', '888')]:
            result = await strict.send('discord', config, '555', 'sealed', thread_id=thread,
                prepared_plan=plan, execution_id=execution, claim_id=claim)
            assert not result.get('success'), result
        assert wire.events == []
        result = await strict.send('discord', config, '555', 'sealed', thread_id='777',
            prepared_plan=plan, execution_id='exec', claim_id='claim')
        assert result.get('success'), result
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_consumption_releases_payload_and_has_no_shared_capacity(monkeypatch):
    from tools import send_message_strict_discord as discord
    original = discord.prepare
    references = []

    def observed(*args, **kwargs):
        payload = original(*args, **kwargs)
        references.append(weakref.ref(payload))
        return payload

    monkeypatch.setattr(discord, 'prepare', observed)
    config = PlatformConfig(token='inert')
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'discord')
        for index in range(34):
            text = str(index)
            plan = strict.prepare('discord', config, '555', text)
            with pytest.raises(FrozenInstanceError):
                plan.binding = None
            result = await strict.send('discord', config, '555', text, prepared_plan=plan)
            assert result.get('success'), result
        gc.collect()
        assert all(reference() is None for reference in references)
        assert not hasattr(config, '_strict_standalone_plans')
        assert len(wire.events) == 34


@pytest.mark.asyncio
async def test_concurrent_prepares_same_config_path_text_keep_distinct_media_bytes(tmp_path, monkeypatch):
    from pathlib import Path
    config = PlatformConfig(token='inert')
    path = tmp_path / 'same.txt'
    path.write_bytes(b'first snapshot')
    media = [(str(path), False)]
    original_read = Path.read_bytes
    first_frozen = threading.Event()
    both_frozen = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = []

    def read(source):
        value = original_read(source)
        if source == path:
            with reads_lock:
                reads.append(value.decode())
                if len(reads) == 1:
                    path.write_bytes(b'second snapshot')
                    first_frozen.set()
            both_frozen.wait(timeout=10)
        return value

    monkeypatch.setattr(Path, 'read_bytes', read)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'discord')
        def prepare(second):
            if second:
                assert first_frozen.wait(timeout=10)
            return strict.prepare('discord', config, '555', 'same caption', media_files=media)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(prepare, False)
            second = pool.submit(prepare, True)
            plans = await asyncio.to_thread(lambda: [first.result(timeout=15), second.result(timeout=15)])
        assert reads == ['first snapshot', 'second snapshot']
        for plan in plans:
            result = await strict.send('discord', config, '555', 'same caption', media_files=media, prepared_plan=plan)
            assert result.get('success'), result
        uploaded = [next(part['body'] for part in event['parts'] if part['filename']) for event in wire.events]
        assert uploaded == ['first snapshot', 'second snapshot']
        assert not hasattr(config, '_strict_standalone_plans')
