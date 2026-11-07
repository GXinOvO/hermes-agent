"""Plan freezing and all-media preflight at the public standalone boundary."""
import json

import pytest

from gateway.config import PlatformConfig
from tools import send_message_strict as strict
from tests.tools import test_strict_standalone_dispatch as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_prepare_freezes_token_and_payload_before_claim(monkeypatch, platform):
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert-preclaim')
        plan = strict.prepare(platform, config, fixtures.target(platform), 'sealed', thread_id='777')
        config.token = 'inert-after-claim'
        result = await strict.send(platform, config, fixtures.target(platform), 'sealed', thread_id='777', prepared_plan=plan)
        assert result.get('success'), result
        assert wire.events[0]['authorization'].endswith('inert-preclaim')


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_prepare_missing_later_media_rejects_whole_plan(tmp_path, monkeypatch, platform):
    file = tmp_path / 'first.txt'
    file.write_text('sealed file')
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        with pytest.raises(RuntimeError, match='media unavailable'):
            strict.prepare(platform, PlatformConfig(token='inert'), fixtures.target(platform), 'sealed',
                           media_files=[(str(file), False), (str(tmp_path / 'missing.txt'), False)])
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_media_bytes_are_sealed_before_text_or_upload(tmp_path, monkeypatch, platform):
    file = tmp_path / 'first.txt'
    file.write_text('original bytes')
    media = [(str(file), False)]
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        plan = strict.prepare(platform, config, fixtures.target(platform), 'caption', media_files=media, thread_id='777')
        file.write_text('changed after claim')
        result = await strict.send(platform, config, fixtures.target(platform), 'caption', media_files=media, thread_id='777', prepared_plan=plan)
        assert result.get('success'), result
        raw = json.dumps(wire.events)
        assert any(event['body_sha256'] == __import__('hashlib').sha256(b'original bytes').hexdigest()
                   for event in wire.events) if platform == 'slack' else 'original bytes' in raw
        assert 'changed after claim' not in raw
        assert len(wire.events) == (3 if platform == 'slack' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_send_rejects_thread_drift_after_prepare(monkeypatch, platform):
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert')
        plan = strict.prepare(platform, config, fixtures.target(platform), 'sealed', thread_id='777')
        result = await strict.send(platform, config, fixtures.target(platform), 'sealed', thread_id='888', prepared_plan=plan)
        assert not result.get('success')
        assert wire.events == []


@pytest.mark.asyncio
async def test_slack_selects_saved_workspace_before_claim(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    saved = get_hermes_home() / 'slack_tokens.json'
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({'T1': {'token': 'inert-first'}, 'T2': {'token': 'inert-selected'}}))
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        config = PlatformConfig(extra={'team_id': 'T2'})
        plan = strict.prepare('slack', config, 'C555', 'sealed')
        saved.write_text(json.dumps({'T2': {'token': 'inert-changed'}}))
        result = await strict.send('slack', config, 'C555', 'sealed', prepared_plan=plan)
        assert result.get('success'), result
        assert wire.events[0]['authorization'] == 'Bearer inert-selected'


@pytest.mark.parametrize('platform', ['slack', 'discord'])
def test_no_token_rejects_before_claim(platform):
    with pytest.raises(RuntimeError):
        strict.prepare(platform, PlatformConfig(), fixtures.target(platform), 'sealed')


@pytest.mark.parametrize('target', ['U555', 'user:U555', 'user_name:someone', '#channel'])
def test_slack_unresolved_destination_rejects_without_probe(target):
    with pytest.raises(RuntimeError, match='resolved conversation'):
        strict.prepare('slack', PlatformConfig(token='inert'), target, 'sealed')


def test_slack_ambiguous_workspaces_reject_before_claim():
    with pytest.raises(RuntimeError, match='workspace'):
        strict.prepare('slack', PlatformConfig(token='inert-one,inert-two'), 'C555', 'sealed')


@pytest.mark.asyncio
async def test_slack_saved_workspace_accepts_utf8_bom_before_claim(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    saved = get_hermes_home() / 'slack_tokens.json'
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({'T2':{'token':'inert-selected'}}), encoding='utf-8-sig')
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        config = PlatformConfig(extra={'team_id':'T2'})
        plan = strict.prepare('slack', config, 'C555', 'sealed')
        result = await strict.send('slack', config, 'C555', 'sealed', prepared_plan=plan)
        assert result.get('success') and len(wire.events) == 1
        assert wire.events[0]['authorization'] == 'Bearer inert-selected'

