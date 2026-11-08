"""Discover the protected owned backend through the native plugin loader."""
import json


import pytest

from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home


def test_protected_provider_is_discoverable_and_missing_config_is_closed():
    from agent.terminal_env_registry import get_provider
    from tools.terminal_tool_backends import _create_environment
    home = get_hermes_home()
    (home / 'config.yaml').write_text(json.dumps({
        'plugins': {'enabled': ['owned-process'], 'isolation': 'in_process'},
        'terminal': {'backend': 'protected_owned', 'cwd': '/cas'}}))
    manager = PluginManager()
    manager.discover_and_load()
    try:
        provider = get_provider('protected_owned')
        assert provider is not None, 'protected_owned must be discovered from the shipped plugin'
        assert provider.default_cwd == '/cas'
        assert provider.skip_container_guards is False
        assert provider.is_available() is False
        with pytest.raises((ValueError, RuntimeError), match='protected'):
            _create_environment('protected_owned', image='', cwd='/cas', timeout=5)
    finally:
        manager.unload()
