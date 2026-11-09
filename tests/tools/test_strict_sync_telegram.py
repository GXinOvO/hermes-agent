import pytest

from gateway.config import Platform, PlatformConfig
from gateway.strict_transport import strict_transport_scope
from tools import send_message_strict as strict
from tools.send_message_tool import _send_to_platform
from tests.tools import test_strict_standalone_dispatch as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,notify,silent', [
    ('important', None, True), ('important', False, True), ('important', True, False),
    ('all', None, False), ('all', False, False), ('all', True, False),
])
async def test_telegram_frozen_notification_and_configured_server(tmp_path, monkeypatch, mode, notify, silent):
    from plugins.platforms.telegram import telegram_outbound
    monkeypatch.setattr(telegram_outbound, 'resolve_notifications_mode', lambda: mode)
    file = tmp_path / 'file.txt'
    file.write_text('frozen media')
    media = [(str(file), False)]
    async with fixtures.peer() as wire:
        config = PlatformConfig(token='inert', extra={'base_url': wire.base + '/bot'})
        plan = strict.prepare(Platform.TELEGRAM, config, '555', 'hello', media_files=media,
                              notify=notify, execution_id='exec', claim_id='claim')
        config.extra['base_url'] = 'http://127.0.0.1:1/bot'
        monkeypatch.setattr(telegram_outbound, 'resolve_notifications_mode', lambda: 'all' if mode == 'important' else 'important')
        with strict_transport_scope():
            result = await _send_to_platform(Platform.TELEGRAM, config, '555', 'hello', media_files=media,
                notify=notify, prepared_plan=plan, execution_id='exec', claim_id='claim')
        assert result.get('success'), result
        assert len(wire.events) == 2
        assert all(event['path'].startswith('/botinert/') for event in wire.events)
        text, upload = wire.events
        assert text['form'].get('disable_notification') == (['true'] if silent else None)
        fields = {part['name']: part['body'] for part in upload['parts']}
        assert fields.get('disable_notification') == ('true' if silent else None)


@pytest.mark.asyncio
async def test_notify_drift_is_rejected_before_plan_consumption(monkeypatch):
    from plugins.platforms.telegram import telegram_outbound
    monkeypatch.setattr(telegram_outbound, 'resolve_notifications_mode', lambda: 'important')
    async with fixtures.peer() as wire:
        config = PlatformConfig(token='inert', extra={'base_url': wire.base + '/bot'})
        plan = strict.prepare(Platform.TELEGRAM, config, '555', 'hello', notify=False)
        bad = await strict.send(Platform.TELEGRAM, config, '555', 'hello', notify=True, prepared_plan=plan)
        assert bad.get('error') and not wire.events
        good = await strict.send(Platform.TELEGRAM, config, '555', 'hello', notify=False, prepared_plan=plan)
        assert good.get('success') and len(wire.events) == 1


def test_telegram_proxy_is_selected_for_the_configured_host(monkeypatch):
    from gateway.platforms import base
    captured = []
    monkeypatch.setattr(base, 'resolve_proxy_url', lambda *args, **kwargs: captured.append((args, kwargs)))
    config = PlatformConfig(token='inert', extra={'base_url': 'https://bot.example/bot', 'proxy_url': 'http://proxy.example:8080'})
    strict.prepare(Platform.TELEGRAM, config, '555', 'hello')
    assert captured and captured[0][1]['configured'] == 'http://proxy.example:8080'
    assert captured[0][1]['target_hosts'] == ['bot.example:443']
