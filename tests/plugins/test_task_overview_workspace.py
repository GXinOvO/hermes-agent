import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('task_overview_workspace', Path(__file__).resolve().parents[2] / 'plugins/task-overview/__init__.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


@pytest.mark.parametrize('missing', [False, True])
def test_real_command_dispatch_binds_verification_to_session_workspace(tmp_path, monkeypatch, missing):
    from hermes_state import SessionDB
    from tui_gateway.server import _run_plugin_command
    from agent.verification_evidence import record_terminal_result

    monkeypatch.setenv('HERMES_VERIFY_ON_STOP', '1')
    projects = []
    for name in ['ambient', 'current']:
        project = tmp_path / name
        project.mkdir()
        (project / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
        projects.append(project)
    ambient, current = projects
    monkeypatch.chdir(ambient)
    monkeypatch.setenv('TERMINAL_CWD', str(ambient))
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    db.create_session('workspace-session', source='desktop')
    record_terminal_result(command='pytest -q', cwd=ambient, session_id='workspace-session', exit_code=0, output='1 passed')
    record_terminal_result(command='pytest -q', cwd=current, session_id='workspace-session', exit_code=1, output='1 failed')
    try:
        with patch('hermes_state.SessionDB', lambda *, read_only: SessionDB(path, read_only=read_only)):
            for cwd, expected in ([(None, None)] if missing else [(ambient, 'passed'), (current, 'failed')]):
                data = json.loads(_run_plugin_command(backend.read_command, '{"stored_id":"workspace-session"}', session={'session_key': 'workspace-session', 'cwd': str(cwd) if cwd else ''}, sid='runtime'))
                assert data['ok'] is True
                if missing:
                    assert data['verification'] is None
                    assert data['verification_cwd'] is None
                    assert 'workspace unavailable' in data['verification_error']
                else:
                    assert data['verification']['status'] == expected
                    assert data['verification']['root'] == str(cwd)
                    assert data['verification']['session_id'] == 'workspace-session'
                    assert data['verification_cwd'] == str(cwd)
                    assert data['verification_error'] is None
    finally:
        db.close()
