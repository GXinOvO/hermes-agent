import hashlib
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from cron.memory_audit_policy import RunPolicy
from tests.agent import test_tool_call_guardrail_runtime as fixtures


def invoke(agent, name, args, call_id):
    messages = []
    call = fixtures._mock_tool_call(name, json.dumps(args), call_id)
    agent._execute_tool_calls_sequential(SimpleNamespace(content='', tool_calls=[call]), messages, 'audit-scope-test')
    assert len(messages) == 1
    return json.loads(messages[0]['content'])


def bind_report_store(policy, tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from cron import memory_audit_policy
    from tools import memory_tool

    memories = tmp_path / 'memories'
    memories.mkdir()
    for name in ('MEMORY.md', 'USER.md'):
        (memories / name).write_text(f'{name} fixture', encoding='utf-8')
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    store = memory_tool.MemoryStore()
    policy.snapshot_hashes = {
        target: hashlib.sha256((memories / filename).read_bytes()).hexdigest()
        for target, filename in (('memory', 'MEMORY.md'), ('user', 'USER.md'))
    }
    policy.audit_lock_path = tmp_path / 'cron' / '.memory-audit.lock'
    policy.interpreter_path = Path(sys.executable).resolve()
    policy.interpreter_sha256 = hashlib.sha256(policy.interpreter_path.read_bytes()).hexdigest()
    monkeypatch.setattr(memory_audit_policy, 'approved_interpreter_identity',
                        lambda: (policy.interpreter_path, policy.interpreter_sha256))
    assert policy.bind_store_identity(store)
    return store


def test_partial_cannot_use_terminal_to_write_memory(tmp_path, monkeypatch):
    import tools.terminal_tool
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    target = tmp_path / 'USER.md'
    target.write_text('fixture original', encoding='utf-8')
    command = 'printf %s changed >> ' + shlex.quote(str(target))
    agent = fixtures._make_agent('terminal', platform='cron')
    control = invoke(agent, 'terminal', {'command': command}, 'control')
    assert control.get('exit_code') == 0, control
    assert target.read_text() == 'fixture originalchanged'
    target.write_text('fixture original', encoding='utf-8')
    agent._memory_audit_policy = RunPolicy('partial', {}, None)
    blocked = invoke(agent, 'terminal', {'command': command}, 'guarded')
    assert blocked.get('error') == 'memory_audit_tool_scope_blocked', blocked
    assert target.read_text() == 'fixture original'


def test_audit_direct_store_respects_disabled_target(tmp_path, monkeypatch):
    from tools import memory_tool
    memories = tmp_path / 'memories'
    memories.mkdir()
    (memories / 'MEMORY.md').write_text('memory fact', encoding='utf-8')
    (memories / 'USER.md').write_text('user fact', encoding='utf-8')
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    store = memory_tool.MemoryStore(memory_enabled=True, user_profile_enabled=False)
    policy = RunPolicy('complete', {
        'memory': __import__('hashlib').sha256((memories / 'MEMORY.md').read_bytes()).hexdigest(),
        'user': __import__('hashlib').sha256((memories / 'USER.md').read_bytes()).hexdigest(),
    }, None)
    store._memory_audit_policy = policy
    result = store.add('user', 'must be refused')
    assert result.get('success') is False
    assert result.get('error', '').endswith('writes are disabled in memory config.')
    assert (memories / 'USER.md').read_text(encoding='utf-8') == 'user fact'


def test_audit_noop_add_does_not_consume_target_write_slot(tmp_path, monkeypatch):
    import hashlib
    from tools import memory_tool
    memories = tmp_path / 'memories'
    memories.mkdir()
    user = memories / 'USER.md'
    user.write_text('existing', encoding='utf-8')
    (memories / 'MEMORY.md').write_text('memory fact', encoding='utf-8')
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    policy = RunPolicy('complete', {
        'memory': hashlib.sha256((memories / 'MEMORY.md').read_bytes()).hexdigest(),
        'user': hashlib.sha256(user.read_bytes()).hexdigest(),
    }, None)
    store = memory_tool.MemoryStore()
    store._memory_audit_policy = policy
    assert store.add('user', 'existing').get('success') is True
    written = store.add('user', 'new fact')
    assert written.get('success') is True, written
    assert user.read_text(encoding='utf-8') == 'existing\n§\nnew fact'


def test_audit_rejects_removing_last_entry(tmp_path, monkeypatch):
    import hashlib
    from tools import memory_tool
    memories = tmp_path / 'memories'
    memories.mkdir()
    user = memories / 'USER.md'
    user.write_text('only entry', encoding='utf-8')
    (memories / 'MEMORY.md').write_text('memory fact', encoding='utf-8')
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    policy = RunPolicy('complete', {
        'memory': hashlib.sha256((memories / 'MEMORY.md').read_bytes()).hexdigest(),
        'user': hashlib.sha256(user.read_bytes()).hexdigest(),
    }, None)
    store = memory_tool.MemoryStore()
    store._memory_audit_policy = policy
    result = store.remove('user', 'only entry', 'only entry')
    assert result.get('success') is False
    assert result.get('error') == 'memory_audit_cannot_empty_store'
    assert user.read_text(encoding='utf-8') == 'only entry'


def test_partial_can_read_only_bound_targets(tmp_path):
    import tools.file_tools

    allowed = tmp_path / 'USER.md'
    allowed.write_text('allowed fixture')
    secret = tmp_path / 'unrelated.txt'
    secret.write_text('unrelated sentinel')
    agent = fixtures._make_agent('read_file', platform='cron')
    control = invoke(agent, 'read_file', {'path': str(secret)}, 'read-control')
    assert 'unrelated sentinel' in control.get('content', ''), control
    policy = RunPolicy('partial', {}, None)
    policy.read_paths = {allowed}
    agent._memory_audit_policy = policy
    allowed_result = invoke(agent, 'read_file', {'path': str(allowed)}, 'read-allowed')
    assert 'allowed fixture' in allowed_result.get('content', ''), allowed_result
    blocked = invoke(agent, 'read_file', {'path': str(secret)}, 'read-denied')
    assert blocked.get('error') == 'memory_audit_tool_scope_blocked', blocked
    assert 'unrelated sentinel' not in json.dumps(blocked)
    allowed.unlink()
    allowed.symlink_to(secret)
    linked = invoke(agent, 'read_file', {'path': str(allowed)}, 'read-symlink')
    assert linked.get('error') == 'memory_audit_tool_scope_blocked', linked
    assert 'unrelated sentinel' not in json.dumps(linked)

    secret_dir = tmp_path / 'private-dir'
    secret_dir.mkdir()
    (secret_dir / 'manifest.json').write_text('private manifest')
    linked_dir = tmp_path / 'allowed-dir'
    linked_dir.symlink_to(secret_dir, target_is_directory=True)
    policy.read_paths.add(linked_dir / 'manifest.json')
    parent_link = invoke(agent, 'read_file', {'path': str(linked_dir / 'manifest.json')}, 'parent-symlink')
    assert parent_link.get('error') == 'memory_audit_tool_scope_blocked', parent_link
    assert 'private manifest' not in json.dumps(parent_link)


def test_symlinked_home_binds_audit_write_and_report_to_real_home(tmp_path, monkeypatch):
    from pathlib import Path
    from cron import memory_audit_policy
    from tools import memory_tool

    real_home = tmp_path / 'real-home'
    real_home.mkdir()
    linked_home = tmp_path / 'linked-home'
    linked_home.symlink_to(real_home, target_is_directory=True)
    (linked_home / 'reports').mkdir()
    memories = linked_home / 'memories'
    scripts = linked_home / 'scripts'
    (linked_home / 'cron').mkdir()
    memories.mkdir()
    scripts.mkdir()
    (memories / 'MEMORY.md').write_text('original', encoding='utf-8')
    (memories / 'USER.md').write_text('profile', encoding='utf-8')
    for name in ('builtin-memory-audit-input.py', 'publish-builtin-memory-report.py'):
        (scripts / name).write_bytes((Path(__file__).resolve().parents[2] / 'scripts' / name).read_bytes())
    monkeypatch.setenv('HERMES_HOME', str(linked_home))
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: memories)
    store = memory_tool.MemoryStore()
    store.load_from_disk()
    hashes = {
        target: hashlib.sha256((memories / filename).read_bytes()).hexdigest()
        for target, filename in (('memory', 'MEMORY.md'), ('user', 'USER.md'))
    }
    policy = RunPolicy('complete', hashes, None)
    policy.audit_lock_path = linked_home / 'cron' / '.memory-audit.lock'
    assert policy.bind_store_identity(store)
    store._memory_audit_policy = policy
    assert store.add('memory', 'audit write') ['success'] is True
    policy.report_source = real_home / 'reports' / 'maintenance.md'
    result = json.loads(policy.run_report_action('write_report', {'content': '# report'}))
    assert result.get('success') is True, result
    assert policy.report_source.read_text(encoding='utf-8') == '# report'


def test_partial_can_write_its_own_report_only(tmp_path):
    import tools.file_tools
    report = tmp_path / 'report.md'
    other = tmp_path / 'other.md'
    agent = fixtures._make_agent('write_file', platform='cron')
    control = invoke(agent, 'write_file', {'path': str(other), 'content': 'fixture'}, 'file-control')
    assert not control.get('error'), control
    assert other.read_text() == 'fixture'
    other.unlink()
    policy = RunPolicy('partial', {}, None)
    policy.report_source = report
    agent._memory_audit_policy = policy
    blocked = invoke(agent, 'write_file', {'path': str(other), 'content': 'forbidden'}, 'file-denied')
    assert blocked.get('error') == 'memory_audit_tool_scope_blocked', blocked
    assert not other.exists()
    result = invoke(agent, 'write_file', {'path': str(report), 'content': '# Fixture report'}, 'report-write')
    assert not result.get('error'), result
    assert report.read_text() == '# Fixture report'


def test_report_write_same_content_is_idempotent_after_retry(tmp_path):
    policy = RunPolicy('partial', {}, None)
    report = tmp_path / 'report.md'
    policy.report_source = report
    first = json.loads(policy.run_report_action('write_report', {'content': '# Fixture report'}))
    second = json.loads(policy.run_report_action('write_report', {'content': '# Fixture report'}))
    assert first.get('success') is True, first
    assert second.get('success') is True, second
    assert second.get('idempotent') is True, second
    assert policy.report_verified() is False
    assert report.read_text(encoding='utf-8') == '# Fixture report'


def test_report_write_different_content_retries_are_rejected(tmp_path):
    policy = RunPolicy('partial', {}, None)
    report = tmp_path / 'report.md'
    policy.report_source = report
    assert json.loads(policy.run_report_action('write_report', {'content': 'original'})).get('success') is True
    result = json.loads(policy.run_report_action('write_report', {'content': 'replacement'}))
    assert result.get('error'), result
    assert report.read_text(encoding='utf-8') == 'original'


@pytest.mark.platforms('posix')
def test_report_write_retry_rejects_symlink_and_fifo(tmp_path):
    policy = RunPolicy('partial', {}, None)
    outside = tmp_path / 'outside.md'
    outside.write_text('outside', encoding='utf-8')
    symlink = tmp_path / 'symlink.md'
    symlink.symlink_to(outside)
    policy.report_source = symlink
    linked = json.loads(policy.run_report_action('write_report', {'content': 'replacement'}))
    assert linked.get('error'), linked
    assert outside.read_text(encoding='utf-8') == 'outside'
    fifo = tmp_path / 'report.fifo'
    os.mkfifo(fifo)
    policy.report_source = fifo
    blocked = json.loads(policy.run_report_action('write_report', {'content': 'replacement'}))
    assert blocked.get('error'), blocked


def test_bound_report_write_refuses_replaced_parent_symlink(tmp_path):
    import tools.file_tools

    report_dir = tmp_path / 'bound-report-dir'
    outside = tmp_path / 'outside'
    report_dir.mkdir()
    outside.mkdir()
    policy = RunPolicy('partial', {}, None)
    policy.report_source = report_dir / 'report.md'
    report_dir.rmdir()
    report_dir.symlink_to(outside, target_is_directory=True)
    agent = fixtures._make_agent('write_file', platform='cron')
    agent._memory_audit_policy = policy
    result = invoke(agent, 'write_file', {'path': str(policy.report_source), 'content': 'must not escape'}, 'bad-parent')
    assert result.get('error'), result
    assert not (outside / 'report.md').exists()


def test_partial_can_call_only_its_exact_publisher(tmp_path, monkeypatch):
    import sys
    import tools.terminal_tool
    source = tmp_path / 'report.md'
    source.write_text('# Fixture report', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    output = tmp_path / 'published.txt'
    publisher.write_text(
        'import pathlib,sys\n'
        f'p=pathlib.Path({str(output)!r})\n'
        "assert sys.argv[1] == '--source' and sys.argv[3] == '--run-time'\n"
        'p.write_bytes(__memory_audit_source_bytes__)\n'
        "print('{\"published\": true}')\n",
        encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.run_time = '2026-11-12T16:00:00+08:00'
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    agent = fixtures._make_agent('terminal', platform='cron')
    agent._memory_audit_policy = policy
    command = shlex.join([sys.executable, str(publisher), '--source', str(source), '--run-time', policy.run_time])
    injected = invoke(agent, 'terminal', {'command': command + ' ; touch ' + shlex.quote(str(tmp_path / 'extra'))}, 'publish-injection')
    assert injected.get('error') == 'memory_audit_tool_scope_blocked', injected
    assert not output.exists()
    assert not (tmp_path / 'extra').exists()
    published = invoke(agent, 'terminal', {'command': command}, 'publish-allowed')
    assert published.get('exit_code') == 0, published
    assert output.read_text() == '# Fixture report'


def test_publisher_path_replaced_after_check_does_not_execute_new_file(tmp_path, monkeypatch):
    import subprocess
    import sys

    source = tmp_path / 'report.md'
    source.write_text('# Fixture report', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    marker = tmp_path / 'publisher-result'
    publisher.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("approved")\nprint("{{}}")\n')
    replacement = tmp_path / 'replacement.py'
    replacement.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("replaced")\nprint("{{}}")\n')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    original_run = subprocess.Popen

    def swap_before_exec(*args, **kwargs):
        if str(publisher) in args[0]:
            replacement.replace(publisher)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', swap_before_exec)
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('exit_code') == 0, result
    assert marker.read_text() == 'approved'


def test_publisher_refuses_changed_report_source(tmp_path, monkeypatch):
    source = tmp_path / 'report.md'
    source.write_text('# Approved report', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    marker = tmp_path / 'executed'
    publisher.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    source.write_text('# Unapproved changed report', encoding='utf-8')
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('error'), result
    assert not marker.exists()


def test_publisher_does_not_publish_source_replaced_at_launch(tmp_path, monkeypatch):
    import subprocess

    source = tmp_path / 'report.md'
    source.write_text('# Approved report', encoding='utf-8')
    replacement = tmp_path / 'replacement.md'
    replacement.write_text('# Unapproved changed report', encoding='utf-8')
    marker = tmp_path / 'published'
    publisher = tmp_path / 'publisher.py'
    publisher.write_text(
        'import pathlib\n'
        f'pathlib.Path({str(marker)!r}).write_bytes(__memory_audit_source_bytes__)\n'
        'print("{}")\n', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    original_run = subprocess.Popen

    def swap_before_exec(*args, **kwargs):
        if str(publisher) in args[0]:
            replacement.replace(source)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', swap_before_exec)
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('exit_code') == 0, result
    assert marker.read_text() == '# Approved report'


def test_publish_serializes_cooperative_memory_writer(tmp_path, monkeypatch):
    fcntl = pytest.importorskip('fcntl')
    import subprocess
    import threading
    import hermes_constants
    from tools.memory_tool_store import MemoryStore

    source = tmp_path / 'report.md'
    source.write_text('# Fixture report', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    publisher.write_text('print("{}")\n', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    store = bind_report_store(policy, tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    publisher_started = threading.Event()
    release_publisher = threading.Event()
    writer_waiting = threading.Event()
    writer_done = threading.Event()
    publish_results = []
    write_results = []
    real_run = subprocess.Popen
    real_flock = fcntl.flock

    def delayed_publisher(*args, **kwargs):
        publisher_started.set()
        if not release_publisher.wait(5):
            raise TimeoutError('publisher test was not released')
        return real_run(*args, **kwargs)

    def tracked_flock(fd, operation):
        if threading.current_thread().name == 'audit-competing-writer' and operation == fcntl.LOCK_EX:
            writer_waiting.set()
        return real_flock(fd, operation)

    def write_memory():
        try:
            write_results.append(MemoryStore().add('user', 'concurrent independent update'))
        finally:
            writer_done.set()

    monkeypatch.setattr(subprocess, 'Popen', delayed_publisher)
    monkeypatch.setattr(fcntl, 'flock', tracked_flock)
    publisher_thread = threading.Thread(target=lambda: publish_results.append(
        json.loads(policy.run_report_action('publish_report', {}))), daemon=True)
    writer_thread = threading.Thread(target=write_memory, name='audit-competing-writer', daemon=True)
    try:
        publisher_thread.start()
        assert publisher_started.wait(5)
        writer_thread.start()
        assert writer_waiting.wait(5)
        assert not writer_done.is_set()
    finally:
        release_publisher.set()
        publisher_thread.join(10)
        if writer_thread.ident is not None:
            writer_thread.join(10)
    assert not publisher_thread.is_alive() and not writer_thread.is_alive()
    assert publish_results[0].get('exit_code') == 0, publish_results
    assert write_results[0].get('success') is True, write_results
    assert not policy.matches_store(store)


def test_publisher_uses_bound_interpreter(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from cron import memory_audit_policy

    approved = Path('/usr/bin/python3').resolve()
    if not approved.is_file() or os.path.samefile(approved, sys.executable):
        pytest.skip('a distinct Python interpreter is required')
    source = tmp_path / 'report.md'
    source.write_text('# Fixture report', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    publisher.write_text('print("{}")\n', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    policy.interpreter_path = approved
    policy.interpreter_sha256 = hashlib.sha256(approved.read_bytes()).hexdigest()
    monkeypatch.setattr(memory_audit_policy, 'approved_interpreter_identity',
                        lambda: (approved, policy.interpreter_sha256))
    real_run = subprocess.Popen
    observed = []

    def inspect_exec(argv, **kwargs):
        if str(publisher) in argv:
            observed.append(os.path.samefile(argv[6], approved))
        else:
            assert os.path.samefile(argv[0], approved)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', inspect_exec)
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('exit_code') == 0, result
    assert observed == [True]


def test_publisher_can_launch_catalog_with_bound_interpreter(tmp_path, monkeypatch):
    source = tmp_path / 'report.md'
    source.write_text('# Fixture report', encoding='utf-8')
    catalog = tmp_path / 'catalog.py'
    marker = tmp_path / 'catalog-ran'
    catalog.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("ok")\n', encoding='utf-8')
    publisher = tmp_path / 'publisher.py'
    publisher.write_text(
        'import subprocess,sys\n'
        f'catalog = subprocess.run([sys.executable, {str(catalog)!r}], capture_output=True, text=True, pass_fds=(__memory_audit_interpreter_fd__,))\n'
        'print(catalog.stderr, file=sys.stderr)\n'
        'print("{}")\n'
        'raise SystemExit(catalog.returncode)\n', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    result = json.loads(policy.run_report_action('publish_report', {}))
    assert result.get('exit_code') == 0, result
    assert marker.read_text() == 'ok'


def test_pinned_collector_rejects_inplace_change_before_exec(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from cron import scheduler_script

    collector = tmp_path / 'collector.py'
    collector.write_text("print('approved')\n", encoding='utf-8')
    script_digest = hashlib.sha256(collector.read_bytes()).hexdigest()
    interpreter = Path(sys.executable).resolve()
    interpreter_digest = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    monkeypatch.setattr(scheduler_script, '_resolve_script_path', lambda _: (collector, None))
    monkeypatch.setattr(scheduler_script, '_script_argv', lambda path, _: ([sys.executable, str(path)], {}, None))
    monkeypatch.setattr(scheduler_script, 'resolve_script_interpreter_identity',
                        lambda job, path: (interpreter, interpreter_digest, None))
    original = scheduler_script._run_job_script

    def rewrite_and_run(*args, **kwargs):
        collector.write_text("print('replaced')\n", encoding='utf-8')
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler_script, '_run_job_script', rewrite_and_run)
    success, output = scheduler_script._run_job_script_with_claim_heartbeat(
        {}, 'collector.py', workdir=str(tmp_path), expected_script_sha256=script_digest,
        expected_interpreter_path=str(interpreter), expected_interpreter_sha256=interpreter_digest)
    assert not success or output == 'approved', (success, output)
    assert output != 'replaced'


@pytest.mark.parametrize('binding', ['bound', 'unbound', 'catalog_failure', 'library_failure'])
def test_candidate_publisher_uses_verified_source_snapshot(tmp_path, monkeypatch, capsys, binding):
    import importlib.util
    import io
    from pathlib import Path

    publisher_path = Path(__file__).resolve().parents[2] / 'scripts' / 'publish-builtin-memory-report.py'
    from cron import memory_audit_policy
    assert hashlib.sha256(publisher_path.read_bytes()).hexdigest() == memory_audit_policy.PUBLISHER_SHA256
    spec = importlib.util.spec_from_file_location('isolated_memory_publisher', publisher_path)
    assert spec is not None and spec.loader is not None
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    internal = tmp_path / 'internal'
    reports = tmp_path / 'reports'
    internal.mkdir()
    reports.mkdir()
    renderer = tmp_path / 'renderer.py'
    renderer.write_text(
        'def split_front_matter(front): return ({"title": "test"}, front)\n'
        'def render_sections(lines): return ("\\n".join(lines), ["heading"])\n'
        'def build_page(meta, body, toc): return "<html>" + body + "</html>"\n', encoding='utf-8')
    monkeypatch.setattr(publisher, 'INTERNAL', internal)
    monkeypatch.setattr(publisher, 'REPORTS', reports)
    monkeypatch.setattr(publisher, 'RENDERER', renderer)
    monkeypatch.setattr(publisher, 'CATALOG', tmp_path / 'catalog.py')
    from cron import memory_audit_artifacts as artifacts
    helper = str(Path(artifacts.__file__).resolve())
    generation = tmp_path / 'catalog/generation.json'
    monkeypatch.setattr(publisher, 'CATALOG_RECEIPT', generation)
    if binding != 'unbound':
        monkeypatch.setattr(publisher, '__memory_audit_interpreter_fd__', 0, raising=False)
        monkeypatch.setattr(publisher, '__memory_audit_dependencies__',
                            {str(publisher.CATALOG): b'print("{}")\n', helper: Path(helper).read_bytes()}, raising=False)
        monkeypatch.setattr(publisher, '__memory_audit_artifacts__', artifacts, raising=False)
        monkeypatch.setattr(publisher, '__memory_audit_artifact_path__', helper, raising=False)
        monkeypatch.setattr(publisher, '__memory_audit_catalog_bootstrap__',
                            memory_audit_policy._VERIFIED_CATALOG_BOOTSTRAP, raising=False)
    def catalog_run(*args, **kwargs):
        if binding != 'catalog_failure':
            index = reports / 'index.html'
            index.write_text('<html>catalog fixture</html>')
            generation.parent.mkdir()
            generation.write_text(json.dumps({'output_html': str(index),
                'html_sha256': hashlib.sha256(index.read_bytes()).hexdigest(), 'sources': {}}))
        output = artifacts.catalog_readback(reports, generation, target,
            artifacts.raw_manifest(publisher.__memory_audit_dependencies__),
            artifacts.input_manifest(artifacts.snapshot_catalog_inputs(reports))) if binding != 'catalog_failure' else {}
        return SimpleNamespace(returncode=7 if binding == 'catalog_failure' else 0, stderr='fixture catalog failure',
                               stdout=json.dumps(output))

    monkeypatch.setattr(publisher.subprocess, 'run', catalog_run)
    source = internal / 'maintenance.md'
    source.write_text('Replacement MEMORY.md USER.md', encoding='utf-8')
    verified = b'Approved MEMORY.md USER.md'
    stamp = '2026-11-12T16:00:00+08:00'
    target = reports / '2026-11-07_2026-11-13' / '2026-11-12' / '记忆维护-2026-11-12-160000.html'
    relative = target.relative_to(reports).as_posix()

    def library_response(request, timeout):
        if binding == 'library_failure':
            raise OSError('fixture library unavailable')
        if isinstance(request, str):
            return io.BytesIO(target.read_bytes())
        data = {'sources': [{'kind': 'directory', 'path': str(reports), 'id': 'local'}],
                'documents': [{'sourceId': 'local', 'relativePath': relative,
                               'id': 'synthetic', 'filename': target.name}], 'scanWarnings': []}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(publisher, 'urlopen', library_response)
    if binding == 'unbound':
        with pytest.raises(ValueError, match='catalog_dependency_not_bound'):
            publisher.publish(str(source), stamp, verified)
        assert list(reports.rglob('*.html')) == []
        assert list(internal.glob('publication-*.json')) == []
        return
    if binding in ('catalog_failure', 'library_failure'):
        assert publisher.publish(str(source), stamp, verified) == 2
        receipt = json.loads(capsys.readouterr().out)
        saved = internal / 'publication-20261112-160000.json'
        assert json.loads(saved.read_text()) == receipt
        assert receipt['library_indexed'] is False
        assert 'library_document_id' not in receipt
        assert receipt['catalog_exit_code'] == (7 if binding == 'catalog_failure' else 0)
        assert target.is_file()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt['html_sha256']
        assert source.read_text() == 'Replacement MEMORY.md USER.md'
        return
    assert publisher.publish(str(source), stamp, verified) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert 'Approved MEMORY.md USER.md' in target.read_text()
    assert 'Replacement' not in target.read_text()
    assert receipt['source'] == str(source)
    assert receipt['source_sha256'] == hashlib.sha256(verified).hexdigest()
    assert receipt['html_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert receipt['library_preview_sha256'] == receipt['html_sha256']
    assert source.read_text() == 'Replacement MEMORY.md USER.md'


def test_candidate_publisher_process_runs_catalog_in_isolation(tmp_path, monkeypatch):
    import json
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from cron import memory_audit_policy

    internal = tmp_path / 'internal'
    reports = tmp_path / 'reports'
    internal.mkdir()
    reports.mkdir()
    renderer = tmp_path / 'renderer.py'
    renderer.write_text(
        'def split_front_matter(front): return ({"title": "test"}, front)\n'
        'def render_sections(lines): return ("\\n".join(lines), ["heading"])\n'
        'def build_page(meta, body, toc): return "<html>" + body + "</html>"\n', encoding='utf-8')
    catalog = tmp_path / 'catalog.py'
    marker = tmp_path / 'catalog-ran'
    nested = tmp_path / 'nested.py'
    nested_marker = tmp_path / 'nested-ran'
    startup = tmp_path / 'catalog-startup.json'
    site_marker = tmp_path / 'sitecustomize-ran'
    shadow_marker = tmp_path / 'shadow-module-ran'
    (tmp_path / 'sitecustomize.py').write_text(
        f'from pathlib import Path\nPath({str(site_marker)!r}).write_text("sitecustomize")\n',
        encoding='utf-8')
    (tmp_path / 'shadow_module.py').write_text(
        f'from pathlib import Path\nPath({str(shadow_marker)!r}).write_text("shadow")\n',
        encoding='utf-8')
    nested.write_text(f'from pathlib import Path\nPath({str(nested_marker)!r}).write_text("nested-approved")\n', encoding='utf-8')
    catalog.write_text(
        'import importlib.util, json, pathlib, sys\n'
        f'spec=importlib.util.spec_from_file_location("nested", {str(nested)!r})\n'
        'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
        'try:\n import shadow_module; shadow="loaded"\n'
        'except ModuleNotFoundError:\n shadow="blocked"\n'
        f'pathlib.Path({str(startup)!r}).write_text(json.dumps({{"isolated": sys.flags.isolated, "no_site": sys.flags.no_site, "safe_path": getattr(sys.flags, "safe_path", 0), "dont_write_bytecode": sys.dont_write_bytecode, "shadow": shadow}}))\n'
        f'from pathlib import Path\nPath({str(marker)!r}).write_text("catalog-approved")\n'
        'def collect_reports(root): return {"reports": []}\n'
        'def build(root, receipt):\n'
        '    import hashlib\n'
        '    index=root/"index.html";index.write_text("<html>catalog</html>")\n'
        '    result={"output_html":str(index),"html_sha256":hashlib.sha256(index.read_bytes()).hexdigest(),"sources":{}}\n'
        '    receipt.parent.mkdir(parents=True);receipt.write_text(json.dumps(result));return result\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    source = internal / 'maintenance.md'
    source.write_text('Approved MEMORY.md USER.md', encoding='utf-8')
    stamp = '2026-11-12T16:00:00+08:00'
    target = reports / '2026-11-07_2026-11-13' / '2026-11-12' / '记忆维护-2026-11-12-160000.html'

    class LibraryHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            assert self.path == '/api/rescan'
            data = {'sources': [{'kind': 'directory', 'path': str(reports), 'id': 'local'}],
                    'documents': [{'sourceId': 'local', 'relativePath': target.relative_to(reports).as_posix(),
                                   'id': 'synthetic', 'filename': target.name}], 'scanWarnings': []}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path.startswith('/preview/synthetic/')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(target.read_bytes())

    server = ThreadingHTTPServer(('127.0.0.1', 0), LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_publisher = Path(__file__).resolve().parents[2] / 'scripts' / 'publish-builtin-memory-report.py'
    assert hashlib.sha256(actual_publisher.read_bytes()).hexdigest() == memory_audit_policy.PUBLISHER_SHA256
    wrapper = tmp_path / 'publisher-wrapper.py'
    wrapper.write_text(
        'import importlib.util, sys\n'
        f'spec=importlib.util.spec_from_file_location("candidate_publisher", {str(actual_publisher)!r})\n'
        'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
        f'module.INTERNAL={str(internal)!r}; module.REPORTS={str(reports)!r}\n'
        'from pathlib import Path\n'
        'module.INTERNAL=Path(module.INTERNAL); module.REPORTS=Path(module.REPORTS)\n'
        f'module.RENDERER=Path({str(renderer)!r}); module.CATALOG=Path({str(catalog)!r})\n'
        f'module.LIBRARY_URL={f"http://127.0.0.1:{server.server_port}"!r}\n'
        'module.__memory_audit_interpreter_fd__=__memory_audit_interpreter_fd__\n'
        'module.__memory_audit_dependencies__=__memory_audit_dependencies__\n'
        'module.__memory_audit_catalog_bootstrap__=__memory_audit_catalog_bootstrap__\n'
        'module.__memory_audit_artifacts__=__memory_audit_artifacts__\n'
        'module.__memory_audit_artifact_path__=__memory_audit_artifact_path__\n'
        'module.__memory_audit_allow_outputs__=__memory_audit_allow_outputs__\n'
        f'module.CATALOG_RECEIPT=Path({str(tmp_path / "catalog-output/generation.json")!r})\n'
        'raise SystemExit(module.publish(sys.argv[2],sys.argv[4],__memory_audit_source_bytes__))\n',
        encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = wrapper
    policy.publisher_sha256 = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    from cron import memory_audit_artifacts
    helper = Path(memory_audit_artifacts.__file__).resolve()
    policy.publisher_dependencies = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in (renderer, catalog, nested, actual_publisher, helper)}
    policy.run_time = stamp
    original_run = memory_audit_policy.subprocess.Popen
    replaced = []

    def replace_dependencies_before_launch(*args, **kwargs):
        if str(wrapper) in args[0]:
            for path in (renderer, catalog, nested):
                path.write_text('raise RuntimeError("replacement executed")\n', encoding='utf-8')
            replaced.append(True)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(memory_audit_policy.subprocess, 'Popen', replace_dependencies_before_launch)
    try:
        result = json.loads(policy.run_report_action('publish_report', {}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert result.get('exit_code') == 0, result
    receipt = json.loads(result['output'])
    assert replaced == [True]
    assert marker.read_text() == 'catalog-approved'
    assert nested_marker.read_text() == 'nested-approved'
    assert json.loads(startup.read_text()) == {
        'isolated': 1, 'no_site': 1, 'safe_path': 1, 'dont_write_bytecode': True,
        'shadow': 'blocked',
    }
    assert not site_marker.exists()
    assert not shadow_marker.exists()
    assert receipt['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt['html_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert receipt['library_indexed'] is True
    assert receipt['library_preview_sha256'] == receipt['html_sha256']


@pytest.mark.parametrize('closure', ['complete', 'omitted'])
def test_publisher_dependency_map_covers_catalog_python_import_closure(tmp_path, monkeypatch, closure):
    from cron import memory_audit_policy
    from pathlib import Path

    transitive = Path('/local_data/huguangyao/workspace/news-briefings/_automation/assets/md2html.py')
    catalog_path = Path('/local_data/huguangyao/workspace/report-automation/render_catalog.py')
    marker = tmp_path / 'actual-closure-imported'
    publisher = tmp_path / 'closure-publisher.py'
    publisher.write_text(
        'import importlib.util,pathlib\n'
        f'spec=importlib.util.spec_from_file_location("actual_catalog",{str(catalog_path)!r})\n'
        'catalog=importlib.util.module_from_spec(spec);spec.loader.exec_module(catalog)\n'
        'spec=importlib.util.spec_from_file_location("actual_news",catalog.NEWS_RENDERER)\n'
        'news=importlib.util.module_from_spec(spec);spec.loader.exec_module(news)\n'
        'assert callable(news.prepare_fonts) and callable(news.BASE.build_page)\n'
        f'pathlib.Path({str(marker)!r}).write_text("complete")\n'
        'print("{}")\n', encoding='utf-8')
    source = tmp_path / 'report.md'
    source.write_text('MEMORY.md USER.md', encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher_dependencies = dict(memory_audit_policy.PUBLISHER_DEPENDENCY_SHA256)
    if closure == 'omitted':
        del policy.publisher_dependencies[str(transitive)]
    policy.run_time = '2026-11-12T16:00:00+08:00'
    result = json.loads(policy.run_report_action('publish_report', {}))
    if closure == 'complete':
        assert result.get('exit_code') == 0, result
        assert marker.read_text() == 'complete'
    else:
        assert result.get('exit_code') != 0, result
        assert 'memory_audit_unpinned_dependency:' + str(transitive) in result.get('stderr', ''), result
        assert not marker.exists()
        assert policy.publication_receipt is None


def test_guarded_publisher_and_catalog_in_isolation(tmp_path, monkeypatch):
    import hashlib
    import sys
    from pathlib import Path
    home = tmp_path / 'home'
    internal = home / 'reports' / 'builtin-memory-audit-trial-20261110'
    internal.mkdir(parents=True)
    reports = tmp_path / 'weekly-reports'
    reports.mkdir()
    stamp = '2026-11-12T16:00:00+08:00'
    source = internal / 'maintenance-20261112-160000.md'
    receipt = tmp_path / 'catalog' / 'generation.json'
    from cron import memory_audit_artifacts as artifacts
    monkeypatch.setattr(artifacts, 'CATALOG_RECEIPT', receipt)
    helper = Path(artifacts.__file__).resolve()
    scripts = home / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    publisher = scripts / 'publish-builtin-memory-report.py'
    publisher.write_text(
        'import argparse,datetime,hashlib,json,pathlib\n'
        "p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--run-time');a=p.parse_args()\n"
        "source=pathlib.Path(a.source);stamp=datetime.datetime.fromisoformat(a.run_time);day=stamp.date()\n"
        "start=day-datetime.timedelta(days=(day.weekday()-5)%7);end=start+datetime.timedelta(days=6)\n"
        f"root=pathlib.Path({str(reports)!r})\n"
        "dest=root/f'{start}_{end}'/str(day)/f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'\n"
        "dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(__memory_audit_source_bytes__);digest=hashlib.sha256(dest.read_bytes()).hexdigest()\n"
        "receipt={'html':str(dest),'html_sha256':digest,'source':str(source),'source_sha256':hashlib.sha256(__memory_audit_source_bytes__).hexdigest(),'run_time':stamp.isoformat(),'report_readback_verified':True,'library_indexed':True,'library_document_id':'isolated','library_preview_sha256':digest,'catalog_exit_code':0,'creation_baseline':__memory_audit_creation_baseline__}\n"
        f"generation=pathlib.Path({str(receipt)!r})\n"
        "generation.parent.mkdir(parents=True,exist_ok=True);index=root/'index.html';index.write_text('<html>catalog fixture</html>')\n"
        "generation.write_text(json.dumps({'output_html':str(index),'html_sha256':hashlib.sha256(index.read_bytes()).hexdigest(),'sources':{}}))\n"
        "artifacts=__memory_audit_artifacts__;receipt.update(artifacts.catalog_readback(root,generation,dest,artifacts.raw_manifest(__memory_audit_dependencies__),artifacts.input_manifest(artifacts.snapshot_catalog_inputs(root))))\n"
        "(source.parent/f'publication-{stamp:%Y%m%d-%H%M%S}.json').write_text(json.dumps(receipt));print(json.dumps(receipt))\n",
        encoding='utf-8')
    policy = RunPolicy('partial', {}, None)
    store = bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source, policy.publisher, policy.run_time = source, publisher, stamp
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    scripts = home / 'scripts'
    policy.publisher_dependencies = {str(helper): hashlib.sha256(helper.read_bytes()).hexdigest()}
    scripts.mkdir(parents=True, exist_ok=True)
    collector = scripts / 'builtin-memory-audit-input.py'
    collector.write_text('# isolated collector\n', encoding='utf-8')
    policy.collector = collector
    policy.collector_sha256 = hashlib.sha256(collector.read_bytes()).hexdigest()
    from cron import memory_audit_policy
    baseline = internal / 'baseline.json'
    baseline.write_text(json.dumps({'profile_home': str(home), 'files': {}}))
    baseline_sha256 = hashlib.sha256(baseline.read_bytes()).hexdigest()
    monkeypatch.setattr(memory_audit_policy, 'BASELINE_SHA256', baseline_sha256)
    policy.creation_baseline = {'path': str(baseline), 'sha256': baseline_sha256}
    agent = fixtures._make_agent('write_file', 'terminal', platform='cron')
    agent._memory_audit_policy = policy
    body = '# Isolated audit\n\n## Result\nMEMORY.md and USER.md unchanged.\n'
    written = invoke(agent, 'write_file', {'path': str(source), 'content': body}, 'real-report-write')
    assert written.get('success') is True, written
    assert source.read_text(encoding='utf-8') == body
    command = shlex.join([sys.executable, str(publisher), '--source', str(source), '--run-time', stamp])
    baseline_original = baseline.read_bytes()
    baseline.write_bytes(baseline_original + b' ')
    refused = invoke(agent, 'terminal', {'command': command}, 'baseline-drift-before-publish')
    assert refused.get('error') == 'memory_audit_baseline_changed', refused
    assert policy.publication_receipt is None
    assert list(reports.rglob('*.html')) == []
    baseline.write_bytes(baseline_original)
    published = invoke(agent, 'terminal', {'command': command}, 'real-report-publish')
    assert published.get('exit_code') == 0, published
    result = json.loads(published['output'])
    target = Path(result['html'])
    assert target == reports / '2026-11-07_2026-11-13' / '2026-11-12' / '记忆维护-2026-11-12-160000.html'
    assert result['source'] == str(source)
    assert result['report_readback_verified'] is True and result['library_indexed'] is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == result['html_sha256']
    assert result['library_preview_sha256'] == result['html_sha256']
    assert receipt == tmp_path / 'catalog' / 'generation.json'

    from cron import memory_audit_policy
    monkeypatch.setattr(memory_audit_policy, 'REPORT_ROOT', reports)
    assert policy.report_verified() is True
    assert policy.report_verified(store) is True
    for artifact in (source, target, baseline, reports / 'index.html', receipt):
        original = artifact.read_bytes()
        artifact.write_bytes(original + b' ')
        assert policy.report_verified() is False
        artifact.write_bytes(original)
        assert policy.report_verified() is True
    saved = source.parent / 'publication-20261112-160000.json'
    original = saved.read_bytes()
    modified = json.loads(original)
    modified['library_indexed'] = False
    saved.write_text(json.dumps(modified))
    assert policy.report_verified() is False
    saved.write_bytes(original)
    assert policy.report_verified() is True
    scripts = home / 'scripts'
    scripts.mkdir(exist_ok=True)
    (scripts / 'publish-builtin-memory-report.py').write_text(publisher.read_text(encoding='utf-8'), encoding='utf-8')
    monkeypatch.setattr(
        memory_audit_policy, 'PUBLISHER_SHA256',
        hashlib.sha256((scripts / 'publish-builtin-memory-report.py').read_bytes()).hexdigest())
    collector_copy = scripts / 'builtin-memory-audit-input.py'
    collector_copy.write_text(collector.read_text(encoding='utf-8'), encoding='utf-8')
    monkeypatch.setattr(
        memory_audit_policy, 'COLLECTOR_SHA256', hashlib.sha256(collector_copy.read_bytes()).hexdigest())
    monkeypatch.setattr(memory_audit_policy, 'PUBLISHER_DEPENDENCY_SHA256', {
        str(helper): hashlib.sha256(helper.read_bytes()).hexdigest()})
    monkeypatch.setattr(memory_audit_policy, 'REPORT_ROOT', reports)
    failure_target, failure_error = memory_audit_policy.failure_report(home, 'memory_audit_collection_error')
    assert failure_error is None, failure_error
    assert failure_target.is_file()
    assert failure_target != target
    failure_html = failure_target.read_text(encoding='utf-8')
    assert '记忆维护失败' in failure_html and 'MEMORY.md' in failure_html and 'USER.md' in failure_html
    assert '来源覆盖和备份状态未能确认' in failure_html
    assert failure_target.read_text(encoding='utf-8')


def test_report_verification_expires_after_published_memory_write(tmp_path):
    import hashlib
    import json
    from pathlib import Path
    from cron import memory_audit_policy

    memory = tmp_path / 'MEMORY.md'
    user = tmp_path / 'USER.md'
    memory.write_text('memory fact', encoding='utf-8')
    user.write_text('user fact', encoding='utf-8')
    policy = RunPolicy('complete', {
        'memory': hashlib.sha256(memory.read_bytes()).hexdigest(),
        'user': hashlib.sha256(user.read_bytes()).hexdigest(),
    }, None)
    policy.report_source = tmp_path / 'maintenance.md'
    policy.report_source.write_text('# report\nMEMORY.md USER.md', encoding='utf-8')
    policy.report_source_sha256 = hashlib.sha256(policy.report_source.read_bytes()).hexdigest()
    policy.publication_receipt = {'source': str(policy.report_source), 'run_time': '2026-11-12T16:00:00+08:00'}
    policy.published_snapshot_hashes = dict(policy.snapshot_hashes)
    policy.publisher = tmp_path / 'publisher.py'
    policy.publisher.write_text('print("{}")', encoding='utf-8')
    policy.publisher_sha256 = hashlib.sha256(policy.publisher.read_bytes()).hexdigest()
    policy.collector = policy.publisher
    policy.collector_sha256 = policy.publisher_sha256
    policy.publisher_dependencies = {}
    assert policy.published_snapshot_hashes == policy.snapshot_hashes
    policy.note_write('memory', b'changed')
    assert policy.publication_receipt is None
    assert policy.published_snapshot_hashes is None
