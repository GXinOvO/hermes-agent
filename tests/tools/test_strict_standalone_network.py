"""Complete HTTP submission counts, including SDK upload phases and human controls."""
import json

import pytest

from gateway.config import PlatformConfig
from tools import send_message_strict as strict
from tests.tools import test_strict_standalone_dispatch as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
@pytest.mark.parametrize('fault', [None, 'lost', '503', '429', 'redirect', 'badack'])
async def test_text_never_retries_or_follows_redirect(monkeypatch, platform, fault):
    async with fixtures.peer([fault]) as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        plan = strict.prepare(platform, config, fixtures.target(platform), 'sealed', thread_id='777')
        result = await strict.send(platform, config, fixtures.target(platform), 'sealed', thread_id='777', prepared_plan=plan)
        assert bool(result.get('success')) is (fault is None), result
        assert len(wire.events) == 1
        assert not result.get('success') or result['message_ids']
        if fault:
            assert result.get('ambiguous') is True


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_later_chunk_unknown_stops_before_next_chunk_and_media(tmp_path, monkeypatch, platform):
    file = tmp_path / 'file.txt'
    file.write_text('must not upload')
    text = 'x' * (80000 if platform == 'slack' else 5000)
    async with fixtures.peer([None, 'lost']) as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        media = [(str(file), False)]
        plan = strict.prepare(platform, config, fixtures.target(platform), text, media_files=media)
        result = await strict.send(platform, config, fixtures.target(platform), text, media_files=media, prepared_plan=plan)
        assert not result.get('success') and result.get('ambiguous') is True
        assert len(result['message_ids']) == 1
        assert len(wire.events) == 2
        assert all('parts' not in event for event in wire.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_chunks_then_media_keep_native_order(tmp_path, monkeypatch, platform):
    media = []
    for index in range(2):
        file = tmp_path / f'{index}.txt'
        file.write_text(f'media-{index}')
        media.append((str(file), False))
    text = 'x' * (80000 if platform == 'slack' else 5000)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        plan = strict.prepare(platform, config, fixtures.target(platform), text, media_files=media, thread_id='777')
        result = await strict.send(platform, config, fixtures.target(platform), text, media_files=media, thread_id='777', prepared_plan=plan)
        assert result.get('success'), result
        assert len(wire.events) == (9 if platform == 'slack' else 5)
        assert all('json' in event for event in wire.events[:3])
        for event in wire.events[:3]:
            assert event['json'].get('thread_ts') == '777' if platform == 'slack' else event['path'] == '/channels/777/messages'
        if platform == 'slack':
            assert [event['path'] for event in wire.events[3:]] == [
                '/files.getUploadURLExternal', '/upload', '/files.completeUploadExternal'] * 2
            assert all(event['query']['thread_ts'] == '777' and event['query']['channel_id'] == 'C555'
                       for event in wire.events if event['path'] == '/files.completeUploadExternal')
        else:
            assert [event['parts'][1]['filename'] for event in wire.events[3:]] == ['0.txt', '1.txt']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', [0, 1, 2])
@pytest.mark.parametrize('fault', ['lost', '503', '429', 'redirect', 'badack'])
async def test_slack_upload_phase_failure_never_continues(tmp_path, monkeypatch, phase, fault):
    files = []
    for index in range(2):
        file = tmp_path / f'{index}.txt'
        file.write_text('inert bytes')
        files.append((str(file), False))
    async with fixtures.peer([None] * phase + [fault]) as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        config = PlatformConfig(token='inert')
        plan = strict.prepare('slack', config, 'C555', '', media_files=files)
        result = await strict.send('slack', config, 'C555', '', media_files=files, prepared_plan=plan)
        assert not result.get('success'), result
        assert len(wire.events) == phase + 1
        assert result.get('ambiguous') is True


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', '503', '429', 'redirect', 'badack'])
async def test_discord_failed_file_never_continues(tmp_path, monkeypatch, fault):
    file = tmp_path / 'file.txt'
    file.write_text('inert bytes')
    files = [(str(file), False)] * 2
    async with fixtures.peer([fault]) as wire:
        fixtures.route(monkeypatch, wire, 'discord')
        config = PlatformConfig(token='inert')
        plan = strict.prepare('discord', config, '555', '', media_files=files)
        result = await strict.send('discord', config, '555', '', media_files=files, prepared_plan=plan)
        assert not result.get('success') and result.get('ambiguous') is True
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_slack_wrong_workspace_error_never_switches_token(monkeypatch):
    async with fixtures.peer(['workspace']) as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        config = PlatformConfig(token='inert-selected')
        plan = strict.prepare('slack', config, 'C555', 'sealed')
        result = await strict.send('slack', config, 'C555', 'sealed', prepared_plan=plan)
        assert not result.get('success')
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_ordinary_missing_media_caption_still_falls_back(tmp_path, monkeypatch, platform):
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        if platform == 'slack':
            from plugins.platforms.slack import adapter
        else:
            from plugins.platforms.discord import adapter
        result = await adapter._standalone_send(PlatformConfig(token='inert-human'), fixtures.target(platform), '',
            media_files=[(str(tmp_path / 'missing.txt'), False)], thread_id='777', caption='ordinary caption')
        assert result.get('success'), result
        assert result.get('warnings')
        assert len(wire.events) == 1
        assert 'ordinary caption' in json.dumps(wire.events)


@pytest.mark.asyncio
async def test_ordinary_slack_upload_sdk_lost_ack_still_retries(tmp_path, monkeypatch):
    from plugins.platforms.slack import adapter
    file = tmp_path / 'file.txt'
    file.write_text('ordinary bytes')
    async with fixtures.peer(['lost']) as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        result = await adapter._standalone_send(PlatformConfig(token='inert-human'), 'C555', '',
            media_files=[(str(file), False)], caption='ordinary caption')
        assert result.get('success'), result
        assert [e['path'] for e in wire.events] == [
            '/files.getUploadURLExternal', '/files.getUploadURLExternal', '/upload', '/files.completeUploadExternal']
