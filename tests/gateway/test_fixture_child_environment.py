"""Gateway integration children keep test dependencies without ambient credentials."""
import os
from pathlib import Path
import subprocess

import pytest

from tests.gateway import test_api_authority_execution, test_clarify_lock_owner, test_shared_clarify

CHECKOUT = Path(__file__).resolve().parents[2]


def checkout_only(value):
    """True when the child imports from this checkout and nowhere else."""
    return value == str(CHECKOUT)


@pytest.mark.parametrize('value, accepted', [
    (str(CHECKOUT), True),
    ('', False),
    ('/tmp/elsewhere', False),
    (str(CHECKOUT) + os.pathsep + '/tmp/elsewhere', False),
], ids=['checkout-only', 'empty', 'foreign-single', 'checkout-plus-foreign'])
def test_checkout_only_import_path_predicate(value, accepted):
    assert checkout_only(value) is accepted


@pytest.mark.parametrize('launch', [
    test_shared_clarify.test_real_shared_clarify_survives_detach,
    test_clarify_lock_owner.test_cli_turn_batch_answered_by_clarify_lock_from_a_second_viewer_end_to_end,
    test_api_authority_execution._probe,
], ids=['single-clarify', 'batch-clarify', 'api-authority'])
def test_fixture_child_keeps_no_install_and_excludes_ambient_credentials(tmp_path, monkeypatch, launch):
    monkeypatch.setenv('OPENAI_API_KEY', 'inert-parent-key-must-not-reach-child')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'inert-other-key-must-not-reach-child')
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    calls = []

    class Captured(Exception):
        pass

    def capture(argv, **kwargs):
        env = kwargs['env']
        calls.append(argv)
        assert env.get('HERMES_DISABLE_LAZY_INSTALLS') == '1', 'child may auto-install and leave the test interpreter'
        assert 'OPENAI_API_KEY' not in env and 'ANTHROPIC_API_KEY' not in env
        assert Path(env['HERMES_HOME']).is_relative_to(tmp_path)
        assert Path(env['HOME']).is_relative_to(tmp_path)
        assert checkout_only(env['PYTHONPATH']), f'child imports from {env["PYTHONPATH"]!r}, not this checkout'
        raise Captured

    monkeypatch.setattr(subprocess, 'run', capture)
    with pytest.raises(Captured):
        launch(tmp_path)
    assert len(calls) == 1
