"""A syntactically valid child report cannot override native incomplete lifecycle."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'skills/autonomous-ai-agents/hermes-agent/scripts/check-delegated-result.py'


@pytest.mark.parametrize('native', [{'truncated': True}, {'exit_reason': 'max_iterations'}])
def test_public_checker_rejects_incomplete_native_result(tmp_path, native):
    artifact = tmp_path / 'output.md'
    artifact.write_text('Existing bounded work.\n')
    body = {'verdict': 'pass', 'summary': 'claims done', 'artifacts': [str(artifact)],
            'findings': [], 'checks': [], 'remaining': []}
    result = {'status': 'completed', 'schema_valid': True, 'summary': json.dumps(body),
              'truncated': False, 'exit_reason': 'completed'}
    result_path = tmp_path / 'result.json'
    spec = importlib.util.spec_from_file_location('lifecycle_material_checker', SCRIPT)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    args = ['--result', str(result_path), '--required-artifact', str(artifact)]
    # Both the Python consumer and the real CLI must accept the same normal output first.
    good = checker.check_result(result, required_artifacts=[str(artifact)], base_dir=tmp_path)
    assert good['accepted'] is True
    result_path.write_text(json.dumps(result))
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    positive = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60, check=False)
    assert positive.returncode == 0, positive.stderr
    assert json.loads(positive.stdout)['accepted'] is True
    bad = copy.deepcopy(result)
    bad.update(native)
    result_path.write_text(json.dumps(bad))
    observed = checker.check_result(bad, required_artifacts=[str(artifact)], base_dir=tmp_path)
    negative = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60, check=False)
    assert observed['accepted'] is False, (native, observed)
    assert 'lifecycle_incomplete' in observed['errors']
    assert negative.returncode == 1, (native, negative.stdout, negative.stderr)
    assert 'lifecycle_incomplete' in json.loads(negative.stdout)['errors']
    assert artifact.read_text() == 'Existing bounded work.\n'
