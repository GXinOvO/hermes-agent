import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cron import memory_audit_policy
from tools import memory_tool
from tools.memory_tool_store import MemoryStore


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('replace_after_check', [False, True])
@pytest.mark.parametrize('dependency_state', ['bound', 'omitted', 'missing', 'changed'])
def test_publisher_dependency_remains_bound_after_check(tmp_path, monkeypatch, replace_after_check, dependency_state):
    home = tmp_path / 'home'
    memories = home / 'memories'
    scripts = home / 'scripts'
    memories.mkdir(parents=True)
    scripts.mkdir()
    (home / 'cron').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    targets = {'memory': memories / 'MEMORY.md', 'user': memories / 'USER.md'}
    for path in targets.values():
        path.write_text('synthetic entry', encoding='utf-8')
    source = home / 'report.md'
    source.write_text('synthetic report', encoding='utf-8')
    marker = tmp_path / 'dependency-executed'
    dependency = scripts / 'renderer.py'
    replacement = scripts / 'replacement.py'
    dependency.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("approved")\n', encoding='utf-8')
    replacement.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("replacement")\n', encoding='utf-8')
    publisher = scripts / 'publisher.py'
    publisher.write_text(
        'import importlib.util\n'
        f'spec=importlib.util.spec_from_file_location("renderer", {str(dependency)!r})\n'
        'module=importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(module)\n'
        'print("{}")\n', encoding='utf-8')
    snapshots = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in targets.items()}
    policy = memory_audit_policy.RunPolicy('complete', snapshots, None)
    policy.audit_lock_path = home / 'cron' / '.memory-audit.lock'
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.publisher_dependencies = {str(dependency): hashlib.sha256(dependency.read_bytes()).hexdigest()}
    if dependency_state == 'omitted':
        policy.publisher_dependencies.clear()
    elif dependency_state == 'missing':
        dependency.unlink()
    elif dependency_state == 'changed':
        dependency.write_text('raise RuntimeError("changed dependency")\n', encoding='utf-8')
    policy.run_time = '2026-11-12T16:00:00+08:00'
    policy.interpreter_path = Path(sys.executable).resolve()
    policy.interpreter_sha256 = hashlib.sha256(policy.interpreter_path.read_bytes()).hexdigest()
    monkeypatch.setattr(memory_audit_policy, 'approved_interpreter_identity',
                        lambda: (policy.interpreter_path, policy.interpreter_sha256))
    assert policy.bind_store_identity(MemoryStore())
    run = subprocess.Popen
    replacements = []

    def execute_after_replacement(*args, **kwargs):
        if replace_after_check and str(publisher) in args[0]:
            os.replace(replacement, dependency)
            replacements.append(True)
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', execute_after_replacement)
    result = json.loads(policy.run_report_action('publish_report', {}))
    if dependency_state in ('missing', 'changed'):
        assert result.get('error') == 'memory_audit_publisher_dependency_changed', result
        assert replacements == []
        assert not marker.exists()
    elif dependency_state == 'omitted':
        assert result.get('exit_code') != 0, result
        assert 'memory_audit_unpinned_dependency' in result.get('stderr', ''), result
        assert not marker.exists()
        assert policy.publication_receipt is None
    elif replace_after_check:
        assert replacements == [True]
        assert not marker.exists() or marker.read_text(encoding='utf-8') == 'approved', result
    else:
        assert result.get('exit_code') == 0, result
        assert marker.read_text(encoding='utf-8') == 'approved'


@pytest.mark.parametrize('map_change', ['missing', 'extra', 'digest'])
def test_production_publisher_requires_exact_dependency_map(tmp_path, monkeypatch, map_change):
    from tests.cron.test_memory_audit_tool_scope import bind_report_store

    publisher = tmp_path / 'publisher.py'
    publisher.write_bytes((Path(__file__).resolve().parents[2] / 'scripts' /
                           'publish-builtin-memory-report.py').read_bytes())
    source = tmp_path / 'report.md'
    source.write_text('MEMORY.md USER.md', encoding='utf-8')
    policy = memory_audit_policy.RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    assert policy.publisher_sha256 == memory_audit_policy.PUBLISHER_SHA256
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    policy.publisher_dependencies = dict(memory_audit_policy.PUBLISHER_DEPENDENCY_SHA256)
    dependency = next(iter(policy.publisher_dependencies))
    if map_change == 'missing':
        del policy.publisher_dependencies[dependency]
    elif map_change == 'extra':
        policy.publisher_dependencies[str(tmp_path / 'extra.py')] = '0' * 64
    else:
        policy.publisher_dependencies[dependency] = '0' * 64
    launches = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: launches.append(args))
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('error') == 'memory_audit_publisher_dependency_changed', result
    assert launches == []
    assert policy.publication_receipt is None
