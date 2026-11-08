import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from gateway.config import PlatformConfig

FIXTURES = Path(__file__).parent / 'fixtures' / 'yuanbao'
SHA = '51ede1f023906114ec06c216c4a7b98e8a45d672'


@contextmanager
def loaded_yuanbao(tmp_path, monkeypatch, *, compat=True, home=None, discover=False):
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = home or tmp_path / 'yuanbao-home'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [yuanbao]\n')
    plugin_root = home / 'plugins'
    source = json.loads((FIXTURES / 'source.json').read_text())
    for name, expected in source['files'].items():
        data = (FIXTURES / (name + '.fixture')).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected
        destination = plugin_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    directory = plugin_root / 'yuanbao'
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(str(home))
    manager = get_plugin_manager()
    try:
        if discover:
            manager.discover_and_load()
        else:
            manifest = parse_manifest_file(directory / 'plugin.yaml', directory, 'user', '')
            assert manifest is not None
            manager._load_plugin(manifest)
        assert manager._plugins['yuanbao'].enabled, manager._plugins['yuanbao'].error
        if compat and not discover:
            root = Path(__file__).resolve().parents[2] / 'plugins' / 'strict-transport-compat'
            manifest = parse_manifest_file(root / 'plugin.yaml', root, 'bundled', '')
            assert manifest is not None
            manager._load_plugin(manifest)
            assert manager._plugins['strict-transport-compat'].enabled, manager._plugins['strict-transport-compat'].error
        from gateway.platform_registry import platform_registry

        entry = platform_registry.get('yuanbao')
        assert entry is not None
        module = sys.modules[entry.adapter_factory.__module__]
        yield manager, module, entry, directory
    finally:
        manager.unload()
        reset_hermes_home_override(token)


def new_adapter():
    from gateway.platform_registry import platform_registry

    adapter = platform_registry.create_adapter('yuanbao', PlatformConfig(extra={
        'app_id': 'inert-app', 'app_secret': 'inert-secret', 'bot_id': 'inert-bot'}))
    assert adapter is not None
    return adapter


async def connected_adapter(module):
    from unittest.mock import AsyncMock

    adapter = new_adapter()
    adapter._connection.open = AsyncMock(return_value=True)
    assert await adapter.connect()
    assert type(adapter) is module.YuanbaoAdapter
    return adapter
