import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from cron import memory_audit_policy


@pytest.fixture
def baseline_collector(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    memories = home / 'memories'
    internal = home / 'reports/builtin-memory-audit-trial-20261110'
    scripts = home / 'scripts'
    for path in (memories, internal, scripts):
        path.mkdir(parents=True)
    files = {}
    for name in ('MEMORY.md', 'USER.md'):
        raw = (name + ' fixture').encode()
        (memories / name).write_bytes(raw)
        files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'entries': [raw.decode()]}
    baseline = internal / 'baseline.json'
    baseline.write_text(json.dumps({'profile_home': str(home), 'files': files}))
    digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(memory_audit_policy, 'BASELINE_SHA256', digest, raising=False)
    script = Path(__file__).resolve().parents[2] / 'scripts/builtin-memory-audit-input.py'
    spec = importlib.util.spec_from_file_location('bound_baseline_collector', script)
    assert spec is not None and spec.loader is not None
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    (scripts / 'publish-builtin-memory-report.py').write_text('fixture publisher')
    monkeypatch.setattr(collector, 'collect_history', lambda *args: {
        'complete_within_declared_scope': False,
    })
    return collector, home, baseline, digest


@pytest.mark.parametrize('change', ['changed', 'symlink', 'missing'])
def test_collector_rejects_unbound_creation_baseline(baseline_collector, change):
    collector, _, baseline, _ = baseline_collector
    original = baseline.read_bytes()
    if change == 'changed':
        data = json.loads(original)
        data['files']['USER.md']['entries'] = ['untrusted replacement']
        baseline.write_text(json.dumps(data))
    elif change == 'symlink':
        replacement = baseline.with_name('same-bytes.json')
        replacement.write_bytes(original)
        baseline.unlink()
        baseline.symlink_to(replacement)
    else:
        baseline.unlink()
    with pytest.raises(RuntimeError, match='memory_audit_baseline_changed'):
        collector.collect(preview=True)


def test_collector_includes_bound_baseline_identity(baseline_collector):
    collector, _, baseline, digest = baseline_collector
    payload = collector.collect(preview=True)
    assert payload['creation_baseline'] == {'path': str(baseline), 'sha256': digest}


def test_collection_refuses_changed_baseline_after_capture(baseline_collector):
    collector, home, baseline, _ = baseline_collector
    payload = collector.collect(preview=True)
    payload['history'] = {
        'complete_within_declared_scope': False, 'eligible_messages': 1, 'included_messages': 0,
        'omitted_messages': 0, 'conflict_count': 0, 'uncertain_source_count': 1,
        'omitted_conflicts': 0, 'omitted_uncertain_sources': 0, 'messages': [],
    }
    output = json.dumps(payload)
    assert memory_audit_policy.collection_status((True, output), memory_audit_policy.prompt_output(output), home) == 'partial'
    baseline.write_bytes(baseline.read_bytes() + b'\n')
    assert memory_audit_policy.collection_status((True, output), memory_audit_policy.prompt_output(output), home) == 'error'


@pytest.mark.parametrize('change', ['unchanged', 'changed', 'symlink'])
def test_execution_binding_requires_the_pinned_baseline(baseline_collector, monkeypatch, change):
    import sys

    _, home, baseline, digest = baseline_collector
    scripts = home / 'scripts'
    collector = scripts / memory_audit_policy.SCRIPT_NAME
    collector.write_bytes((Path(__file__).resolve().parents[2] / 'scripts' / collector.name).read_bytes())
    publisher = scripts / 'publish-builtin-memory-report.py'
    monkeypatch.setattr(memory_audit_policy, 'PUBLISHER_SHA256', hashlib.sha256(publisher.read_bytes()).hexdigest())
    monkeypatch.setattr(memory_audit_policy, 'PROFILE_HOME', home)
    monkeypatch.setattr(memory_audit_policy, 'PROMPT_SHA256', hashlib.sha256(b'fixture prompt').hexdigest())
    interpreter = Path(sys.executable).resolve()
    interpreter_digest = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    monkeypatch.setattr(memory_audit_policy, 'approved_interpreter_identity', lambda: (interpreter, interpreter_digest))
    job = {
        'id': memory_audit_policy.JOB_ID, 'name': 'builtin-memory-daily-maintenance-trial',
        'prompt': 'fixture prompt', 'script': collector.name, 'no_agent': False,
        'enabled_toolsets': ['memory', 'file', 'terminal', 'no_mcp'], 'context_from': ['self'],
        'deliver': 'local', 'timezone': 'Asia/Shanghai', 'workdir': str(baseline.parent),
        'schedule': {'kind': 'cron', 'expr': '15 3 * * *', 'display': '15 3 * * *'},
    }
    if change == 'changed':
        baseline.write_bytes(baseline.read_bytes() + b' ')
    elif change == 'symlink':
        replacement = baseline.with_name('same-baseline.json')
        baseline.rename(replacement)
        baseline.symlink_to(replacement)
    binding = memory_audit_policy.execution_binding(job, home, interpreter_digest, interpreter)
    if change == 'unchanged':
        assert binding is not None
        assert binding['baseline_sha256'] == digest
    else:
        assert binding is None
