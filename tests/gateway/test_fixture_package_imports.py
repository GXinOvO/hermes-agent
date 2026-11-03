"""Gateway fixture packages resolve using only the declared checkout import path."""
import importlib

import pytest


@pytest.mark.parametrize('module', ['automation_peer', 'bot_authority_peer', 'kanban_owner_probe',
                                    'kanban_recovery_probe', 'local_automation_peer'])
def test_fixture_resolves_transitive_sibling_imports_from_checkout(module):
    imported = importlib.import_module('tests.gateway.fixtures.' + module)
    assert imported.__package__ == 'tests.gateway.fixtures'
