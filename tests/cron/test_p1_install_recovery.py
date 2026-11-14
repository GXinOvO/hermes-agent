import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("posix")
REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO.parent / "apply_p1.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("p1_installer", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child_env(home):
    return dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(REPO))


def test_install_blocks_schedule_update_without_jobs_lock_degradation(tmp_path, monkeypatch):
    from cron import jobs
    from cron.install_gate import install_gate
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    job = jobs.create_job(prompt="schedule guard", schedule="every 1h", deliver="local")
    before = (home / "cron/jobs.json").read_bytes()
    program = """
import sys
from cron import jobs
jobs._JOBS_LOCK_TIMEOUT_SECONDS = .01
try:
    jobs.update_job(sys.argv[1], {'next_run_at':'2020-01-01T00:00:00+00:00'})
except RuntimeError as exc:
    print(type(exc).__name__)
else:
    print('unexpected_write')
"""
    with install_gate(home, exclusive=True):
        result = subprocess.run([sys.executable, "-B", "-c", program, job["id"]],
                                env=child_env(home), capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "InstallInProgress"
    assert (home / "cron/jobs.json").read_bytes() == before
    assert "Proceeding with in-process locking only" not in result.stderr
    assert jobs.update_job(job["id"], {"name": "after-install"})["name"] == "after-install"


def test_installer_death_keeps_persistent_marker_and_blocks_claim(tmp_path, monkeypatch):
    from cron import executions, jobs
    from cron.scheduler_provider import InProcessCronScheduler
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    job = jobs.create_job(prompt="interrupted install", schedule="every 1h", deliver="local")
    program = """
import signal,sys
from pathlib import Path
from cron.install_gate import install_gate
with install_gate(Path(sys.argv[1]), exclusive=True):
    marker = Path(sys.argv[1])/'cron/.install-pending.json'
    marker.write_text('{}')
    print('ready',flush=True)
    signal.pause()
"""
    proc = subprocess.Popen([sys.executable, "-B", "-c", program, str(home)],
                            env=child_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0]
        assert proc.stdout.readline().strip() == "ready"
        proc.kill()
        proc.wait(timeout=5)
        assert proc.returncode == -9
        assert InProcessCronScheduler().claim_fire(job["id"], manual=True) is None
        assert executions.list_executions(job_id=job["id"]) == []
        assert (home / "cron/.install-pending.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_pending_marker_does_not_block_other_profile(tmp_path, monkeypatch):
    from cron import executions, jobs
    from cron.scheduler_provider import InProcessCronScheduler
    homes = [tmp_path / "a", tmp_path / "b"]
    entries = []
    for home in homes:
        monkeypatch.setenv("HERMES_HOME", str(home))
        entries.append(jobs.create_job(prompt="profile fence", schedule="every 1h", deliver="local"))
    (homes[0] / "cron/.install-pending.json").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(homes[1]))
    claimed = InProcessCronScheduler().claim_fire(entries[1]["id"], manual=True)
    assert claimed and claimed["execution_id"]
    assert len(executions.list_executions(job_id=entries[1]["id"])) == 1


def test_pending_marker_does_not_block_same_job_id_in_other_profile(tmp_path, monkeypatch):
    from cron import executions, jobs
    from cron.scheduler_provider import InProcessCronScheduler
    homes = [tmp_path / "same-a", tmp_path / "same-b"]
    entries = []
    for home in homes:
        monkeypatch.setenv("HERMES_HOME", str(home))
        entries.append(jobs.create_job(prompt="same id", schedule="every 1h", deliver="local"))
    entries[1]["id"] = entries[0]["id"]
    monkeypatch.setenv("HERMES_HOME", str(homes[1]))
    jobs.save_jobs([entries[1]])
    (homes[0] / "cron/.install-pending.json").write_text("{}")
    claimed = InProcessCronScheduler().claim_fire(entries[1]["id"], manual=True)
    assert claimed and len(executions.list_executions(job_id=entries[1]["id"])) == 1


def test_install_marker_is_durable_before_publishing_plugin(tmp_path):
    module = load_installer()
    base = tmp_path / "base"
    cron = base / "cron"
    cron.mkdir(parents=True)
    observed = []
    def action():
        marker = cron / ".install-pending.json"
        observed.append(json.loads(marker.read_text()))
        raise RuntimeError("injected before plugin publication")
    with pytest.raises(RuntimeError, match="injected before plugin publication"):
        module.guarded_install(base, action)
    assert observed[0]["state"] == "installing"
    assert (cron / ".install-pending.json").is_file()


def test_timeout_does_not_issue_second_reload(tmp_path, monkeypatch):
    module = load_installer()
    from gateway import control_socket
    calls = []
    def unconfirmed(*args, **kwargs):
        calls.append((args, kwargs))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron/.install-pending.json").write_text(json.dumps({
        "control_home": str(tmp_path), "token": "fixture"}))
    monkeypatch.setattr(control_socket, "query_gateway_control", unconfirmed)
    with pytest.raises(RuntimeError):
        module.reload_plugin(tmp_path, control_home=tmp_path)
    assert len(calls) == 1


def test_reload_owner_proof_fits_request_and_keeps_full_marker(tmp_path, monkeypatch):
    import hashlib
    from cron.install_capability import runtime_identity
    from gateway import control_socket
    module = load_installer()
    owner = {"hermes_home": str(tmp_path), "pid": os.getpid(), "start_time": 1,
             "code_sha": "fixture", "cron_install_gate": control_socket.CRON_INSTALL_GATE_VERSION,
             "cron_install_capability": runtime_identity(home=tmp_path)}
    marker = tmp_path / "cron/.install-pending.json"
    marker.parent.mkdir()
    original = json.dumps({"owner": owner, "control_home": str(tmp_path), "token": "fixture"})
    marker.write_text(original)
    monkeypatch.setattr(module, "gateway_owner", lambda *a, **k: (tmp_path, owner))
    captured = []
    def query(home, verb, **kwargs):
        captured.append({"verb": verb, "id": 1, "protocol": 1, "params": kwargs["params"]})
        return {"reloaded": True, "home": str(tmp_path), "owner": owner,
                "loaded_sources": {"cron-supervisor-report-budget": "candidate"},
                "activations": [{"key": "cron-supervisor-report-budget", "activated_now": {
                    "gateway_transforms": ["transform_llm_output"]}}]}
    monkeypatch.setattr(control_socket, "query_gateway_control", query)
    module.reload_plugin(tmp_path, expected="candidate")
    assert len(captured) == 1
    assert len((json.dumps(captured[0]) + "\n").encode()) <= control_socket._MAX_REQUEST_BYTES
    expected = hashlib.sha256(json.dumps(owner, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True).encode()).hexdigest()
    assert captured[0]["params"]["owner_sha256"] == expected
    assert "owner" not in captured[0]["params"]
    assert marker.read_text() == original


def test_gateway_owner_rejects_pre_gate_runtime(tmp_path, monkeypatch):
    module = load_installer()
    from gateway import control_socket
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: {"pid": os.getpid()})
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        module.gateway_owner(tmp_path)


def test_gateway_owner_requires_exact_identity_and_consumers(tmp_path, monkeypatch):
    module = load_installer()
    from gateway import control_socket
    from cron.install_capability import runtime_identity
    identity = {
        'cron_install_gate': control_socket.CRON_INSTALL_GATE_VERSION, 'cron_install_capability': runtime_identity(),
        'hermes_home': str(tmp_path), 'pid': os.getpid(),
        'start_time': 1, 'code_sha': 'candidate',
    }
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: identity)
    assert module.CRON_INSTALL_GATE_VERSION == control_socket.CRON_INSTALL_GATE_VERSION == identity['cron_install_capability']['version']
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _: 1)
    assert module.gateway_owner(tmp_path) == tmp_path
    bad = dict(identity, cron_install_capability=dict(identity['cron_install_capability'], version=999))
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: bad)
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        module.gateway_owner(tmp_path)


def test_reconciliation_refuses_an_inflight_reload(tmp_path):
    import fcntl
    module = load_installer()
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / ".install-pending.json").write_text('{}')
    with (cron / ".plugins-reload.lock").open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(BlockingIOError):
            module.reconcile(tmp_path, reloader=lambda _: {})
    assert (cron / ".install-pending.json").read_text() == '{}'


def test_killed_actual_installer_can_restore_then_admit_a_new_claim(tmp_path, monkeypatch):
    import hashlib
    from cron import executions, jobs
    from cron.scheduler_provider import InProcessCronScheduler
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    job = jobs.create_job(prompt='real install interruption', schedule='every 1h', deliver='local')
    row = executions.create_execution(job['id'], source='fixture')
    assert executions.finish_execution(row['id'], success=True)
    cron = home / 'cron'
    (cron / 'admissions').mkdir()
    (cron / '.tick.lock').touch()
    with jobs._fire_job_lock(job['id']) as acquired:
        assert acquired
    target = home / 'plugins/cron-supervisor-report-budget/__init__.py'
    target.parent.mkdir(parents=True)
    old, new = b'old plugin\n', b'new plugin\n'
    target.write_bytes(old)
    candidate = tmp_path / 'candidate.py'
    candidate.write_bytes(new)
    sha = lambda data: hashlib.sha256(data).hexdigest()
    program = '''
import importlib.util,signal,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installer',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.JOB=sys.argv[5]
def paused(_):
    print('published',flush=True)
    signal.pause()
m.apply(base=Path(sys.argv[2]),candidate=Path(sys.argv[3]),receipt_dir=Path(sys.argv[4]),
        old=sys.argv[6],new=sys.argv[7],reloader=paused)
'''
    proc = subprocess.Popen([sys.executable, '-B', '-c', program, str(INSTALLER), str(home),
                             str(candidate), str(tmp_path/'receipts'), job['id'], sha(old), sha(new)],
                            env=child_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0]
        assert proc.stdout.readline().strip() == 'published'
        assert target.read_bytes() == new
        proc.kill()
        proc.wait(timeout=5)
        assert InProcessCronScheduler().claim_fire(job['id'], manual=True) is None
        assert len(executions.list_executions(job_id=job['id'])) == 1
        module = load_installer()
        module.JOB = job['id']
        calls = []
        def restored(_):
            calls.append(target.read_bytes())
            return {'reloaded': True}
        result = module.reconcile(home, reloader=restored)
        assert result['status'] == 'reconciled' and calls == [old]
        assert target.read_bytes() == old
        claimed = InProcessCronScheduler().claim_fire(job['id'], manual=True)
        assert claimed and claimed['execution_id']
        assert len(executions.list_executions(job_id=job['id'])) == 2
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_delayed_reconcile_ignores_install_lead_time_gate(tmp_path, monkeypatch):
    import hashlib
    from cron import executions, jobs
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    job = jobs.create_job(prompt='delayed recovery', schedule='every 1h', deliver='local')
    row = executions.create_execution(job['id'], source='fixture')
    assert executions.finish_execution(row['id'], success=True)
    jobs.update_job(job['id'], {'next_run_at': '2020-01-01T00:00:00+00:00'})
    target = home / 'plugins/cron-supervisor-report-budget/__init__.py'
    target.parent.mkdir(parents=True)
    old, new = b'old plugin\n', b'new plugin\n'
    target.write_bytes(new)
    backup = tmp_path / 'receipts/production-before-old.py'
    backup.parent.mkdir()
    backup.write_bytes(old)
    marker = home / 'cron/.install-pending.json'
    marker.write_text(json.dumps({
        'state': 'installing', 'old': hashlib.sha256(old).hexdigest(),
        'new': hashlib.sha256(new).hexdigest(), 'target': str(target),
        'backup': str(backup), 'control_home': str(home), 'token': 'delayed'}))

    module = load_installer()
    module.JOB = job['id']
    result = module.reconcile(home, reloader=lambda _: {'reloaded': True})
    assert result['status'] == 'reconciled'
    assert target.read_bytes() == old
    assert not marker.exists()


@pytest.mark.parametrize("module_name,binding", [
    ("cron.executions", "create_execution"),
    ("cron.jobs", "claim_job_for_fire"),
    ("cron.scheduler", "run_one_job"),
    ("cron.scheduler", "create_execution"),
    ("cron.scheduler", "claim_job_for_fire"),
    ("cron.executions", "install_gate"),
    ("cron.jobs", "install_gate"),
    ("cron.jobs", "_jobs_lock_admitted"),
    ("cron.install_gate", "lock_fd"),
    ("cron.install_gate", "unlock_fd"),
    ("cron.scheduler", "tick"),
])
def test_owner_rejects_dynamic_binding_drift(tmp_path, monkeypatch, module_name, binding):
    import importlib
    from cron.install_capability import runtime_identity
    from gateway import control_socket
    installer = load_installer()
    owner = {"cron_install_gate": control_socket.CRON_INSTALL_GATE_VERSION,
             "cron_install_capability": runtime_identity(), "hermes_home": str(tmp_path),
             "pid": os.getpid(), "start_time": 1, "code_sha": "fixture"}
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: owner)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _: 1)
    assert installer.gateway_owner(tmp_path) == tmp_path
    def replacement(*args, **kwargs):
        return None
    original = getattr(importlib.import_module(module_name), binding)
    replacement.__name__ = original.__name__
    replacement.__qualname__ = original.__qualname__
    replacement.__module__ = original.__module__
    monkeypatch.setattr(importlib.import_module(module_name), binding, replacement)
    # Even identical module/name claims do not identify the implementation.
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        installer.gateway_owner(tmp_path)


def test_serving_provider_and_captured_tick_are_proved(tmp_path, monkeypatch):
    from cron import scheduler, scheduler_provider as sp
    from cron.install_capability import CapabilityUnavailable, runtime_identity
    from types import MethodType
    provider = sp.resolve_cron_scheduler(capability_probe=True)
    provider._install_tick = scheduler.tick
    monkeypatch.setattr(sp, "_install_runtime_provider", provider)
    assert runtime_identity(serving=True) == runtime_identity()
    original = provider.fire_due
    def unsafe_fire(self, *a, **k):
        return True
    provider.fire_due = MethodType(unsafe_fire, provider)
    with pytest.raises(CapabilityUnavailable, match="differs"):
        runtime_identity(serving=True)
    del provider.fire_due
    assert provider.fire_due == original
    provider._install_tick = unsafe_fire
    with pytest.raises(CapabilityUnavailable, match="binding is missing"):
        runtime_identity(serving=True)


def test_external_provider_probe_never_loads_plugins(tmp_path, monkeypatch):
    from cron.install_capability import CapabilityUnavailable, runtime_identity
    from plugins import cron_providers
    (tmp_path / "config.yaml").write_text("cron:\n  provider: external\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(cron_providers, "load_cron_scheduler", lambda *a: calls.append(a))
    with pytest.raises(CapabilityUnavailable, match="would load plugin"):
        runtime_identity()
    assert calls == []


def test_wrapper_closure_identity_uses_actual_wrapper():
    from functools import wraps
    from cron.install_capability import _callable_identity
    def original():
        return True
    def wrapper(value):
        @wraps(original)
        def wrapped():
            return value
        return wrapped
    assert _callable_identity(wrapper("safe")) != _callable_identity(wrapper("unsafe"))


def test_capability_is_stable_in_another_interpreter(tmp_path):
    from cron.install_capability import runtime_identity
    program = "from cron.install_capability import runtime_identity; import json; print(json.dumps(runtime_identity()))"
    result = subprocess.run([sys.executable, "-B", "-c", program], env=child_env(tmp_path),
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == runtime_identity(home=tmp_path)


def _target_fixture(tmp_path):
    import hashlib
    from contextlib import closing
    from datetime import datetime, timedelta, UTC
    import sqlite3
    module = load_installer()
    home = tmp_path / "profile"
    cron = home / "cron"
    (cron / "admissions").mkdir(parents=True)
    import uuid
    key = f"{cron.resolve()}::{module.JOB}"
    for name in (".tick.lock", ".jobs.lock", f".fire-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.lock"):
        (cron / name).touch()
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{
        "id": module.JOB, "enabled": True, "state": "scheduled",
        "next_run_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}]}))
    with closing(sqlite3.connect(cron / "executions.db")) as conn, conn:
        conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT)")
    target = home / "plugins/cron-supervisor-report-budget/__init__.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old\n")
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(b"new\n")
    arguments = dict(base=home, candidate=candidate, receipt_dir=tmp_path / "receipts",
                     old=hashlib.sha256(b"old\n").hexdigest(), new=hashlib.sha256(b"new\n").hexdigest(),
                     reloader=lambda _: {})
    return module, target, arguments


def test_target_writer_in_another_process_blocks_apply_publish_and_reconcile(tmp_path, monkeypatch):
    module, target, arguments = _target_fixture(tmp_path)
    marker = arguments["base"] / "cron/.install-pending.json"
    program = """
import sys
from pathlib import Path
from pm.plugin_target_lock import target_writer_lock
with target_writer_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    proc = subprocess.Popen([sys.executable, "-B", "-c", program, str(target.parent)],
                            env=child_env(arguments["base"]), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0]
        assert proc.stdout.readline().strip() == "locked"
        monkeypatch.setattr(module, "preflight", lambda *a, **k: pytest.fail("preflight ran without target lock"))
        with pytest.raises(BlockingIOError):
            module.apply(**arguments)
        assert not marker.exists() and not arguments["receipt_dir"].exists()
        with pytest.raises(BlockingIOError):
            module.publish(target, b"new\n", 0o600, expected=arguments["old"])
        marker.write_text("{}")
        with pytest.raises(BlockingIOError):
            module.reconcile(arguments["base"], reloader=lambda _: {})
        assert marker.read_text() == "{}" and target.read_bytes() == b"old\n"
    finally:
        proc.communicate("release\n", timeout=5)
    assert proc.returncode == 0


def test_expected_mismatch_refuses_before_publication(tmp_path):
    module, target, arguments = _target_fixture(tmp_path)
    from pm.plugin_target_lock import target_writer_lock
    # Candidate writer participates in the same protocol before the installer acquires it.
    with target_writer_lock(target.parent):
        target.write_bytes(b"foreign\n")
    with pytest.raises(RuntimeError, match="changed concurrently before publish"):
        module.publish(target, b"new\n", 0o600, expected=arguments["old"])
    assert target.read_bytes() == b"foreign\n"
    assert list(target.parent.glob(".report-budget-install-*")) == []


def test_rollback_passes_expected_new_and_preserves_foreign_bytes(tmp_path, monkeypatch):
    module, target, arguments = _target_fixture(tmp_path)
    publish = module.publish
    observed = []
    def injected(path, data, mode, expected=None):
        if path == target and data == b"old\n":
            observed.append(expected)
            target.write_bytes(b"uncooperative foreign\n")
        return publish(path, data, mode, expected=expected)
    monkeypatch.setattr(module, "publish", injected)
    def fail(_):
        raise RuntimeError("activation failed")
    arguments["reloader"] = fail
    with pytest.raises(RuntimeError, match="changed concurrently before publish"):
        module.apply(**arguments)
    assert observed == [arguments["new"]]
    assert target.read_bytes() == b"uncooperative foreign\n"


def test_product_plugin_writers_use_the_same_lock(tmp_path):
    from pm.plugin_target_lock import target_writer_lock
    _, target, arguments = _target_fixture(tmp_path)
    # The same-process context is reentrant; exercise each actual product writer in a child.
    operations = [
        "from hermes_cli.plugins_cmd_remove import _remove_plugin_core; _remove_plugin_core(p)",
        "from hermes_cli.runtime_state import _recover_plugin_publication; from unittest.mock import patch; "
        "a=patch('hermes_cli.runtime_state.dependency_home_root',return_value=p.parent.parent); a.start(); "
        "_recover_plugin_publication(p, {'target':str(p)}, p/'journal')",
        "from pm.publication import StagedPlugin; from types import SimpleNamespace; from unittest.mock import patch; "
        "s=SimpleNamespace(target=p,staged=p,metadata=p.parent/'.install-metadata.json',configs={},staged_digest='x'); "
        "a=patch('pm.publication.selection_snapshot',return_value={}); b=patch('pm.store.tree_digest',return_value='x'); "
        "a.start(); b.start(); StagedPlugin.publish(s,p)",
        "from pm.install import _publish_inactive; from types import SimpleNamespace; "
        "_publish_inactive(SimpleNamespace(target=p))",
        "from pm.install import _commit_selection; from types import SimpleNamespace; "
        "_commit_selection(None,None,SimpleNamespace(target=p),enabled=[],stamp='x',inputs={},current=True,repair=False,explicit=True)",
        "from hermes_cli.profile_distribution import _copy_dist_payload; _copy_dist_payload(p,p.parent.parent,None,False)",
        "from hermes_cli.backup_restore import _extract_member_atomically; _extract_member_atomically(None,'member',p/'__init__.py')",
        "from hermes_cli.backup import _restore_snapshot_entry; _restore_snapshot_entry('plugin',p/'source',p/'__init__.py')",
    ]
    with target_writer_lock(target.parent):
        for operation in operations:
            code = "import sys; from pathlib import Path; p=Path(sys.argv[1]); " + operation
            result = subprocess.run([sys.executable, "-B", "-c", code, str(target.parent)],
                                    env=child_env(arguments["base"]), capture_output=True, text=True,
                                    timeout=10, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr, result.stderr
    assert target.read_bytes() == b"old\n"


def test_archive_and_snapshot_writers_can_publish_with_the_target_lock(tmp_path):
    import zipfile
    from hermes_cli.backup_restore import _extract_member_atomically
    from hermes_cli.backup import _restore_snapshot_entry
    _, target, arguments = _target_fixture(tmp_path)
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("entry", "archive bytes\n")
    with zipfile.ZipFile(archive) as zf:
        _extract_member_atomically(zf, "entry", target)
    assert target.read_bytes() == b"archive bytes\n"
    assert _restore_snapshot_entry("plugin", arguments["candidate"], target)
    assert target.read_bytes() == b"new\n"


def test_writer_lock_remains_held_at_replace_and_directory_fsync(tmp_path, monkeypatch):
    import stat
    module, target, arguments = _target_fixture(tmp_path)
    replace, fsync = module.os.replace, module.os.fsync
    checked = []
    program = """
from pathlib import Path
import sys
from pm.plugin_target_lock import target_writer_lock
try:
    with target_writer_lock(Path(sys.argv[1])):
        raise AssertionError('writer entered the publication window')
except BlockingIOError:
    print('blocked')
"""
    def check(stage):
        result = subprocess.run([sys.executable, "-B", "-c", program, str(target.parent)],
                                env=child_env(arguments["base"]), capture_output=True, text=True,
                                timeout=10, check=False)
        assert result.returncode == 0 and result.stdout.strip() == "blocked", result.stderr
        checked.append(stage)
    def guarded_replace(source, destination):
        if Path(destination) == target:
            check("replace")
        return replace(source, destination)
    def guarded_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            check("fsync-directory")
        return fsync(fd)
    monkeypatch.setattr(module.os, "replace", guarded_replace)
    monkeypatch.setattr(module.os, "fsync", guarded_fsync)
    module.publish(target, b"new\n", 0o600, expected=arguments["old"])
    assert checked == ["replace", "fsync-directory"] and target.read_bytes() == b"new\n"


def test_recovery_uses_expected_current_digest(tmp_path, monkeypatch):
    module, target, arguments = _target_fixture(tmp_path)
    def unconfirmed(_):
        raise module.ActivationUnconfirmed("activation unconfirmed")
    arguments["reloader"] = unconfirmed
    with pytest.raises(module.ActivationUnconfirmed):
        module.apply(**arguments)
    publish = module.publish
    observed = []
    def injected(path, data, mode, expected=None):
        if path == target:
            observed.append(expected)
            target.write_bytes(b"uncooperative foreign\n")
        return publish(path, data, mode, expected=expected)
    monkeypatch.setattr(module, "publish", injected)
    with pytest.raises(RuntimeError, match="changed concurrently before publish"):
        module.reconcile(arguments["base"], reloader=lambda _: {})
    assert observed == [arguments["new"]] and target.read_bytes() == b"uncooperative foreign\n"


def test_unregistered_or_custom_serving_provider_cannot_advertise_capability(monkeypatch):
    from cron import scheduler_provider as sp
    from gateway.control_socket import build_identify_payload
    monkeypatch.setattr(sp, "_install_runtime_provider", None)
    assert build_identify_payload()["cron_install_capability"] is None
    class ExternalProvider(sp.InProcessCronScheduler):
        def fire_due(self, *a, **k):
            return True
    monkeypatch.setattr(sp, "_install_runtime_provider", ExternalProvider())
    answer = build_identify_payload()
    assert answer["cron_install_capability"] is None
    assert "custom provider" in answer["cron_install_capability_error"]


def test_dynamic_sys_modules_rebinding_is_refused(monkeypatch):
    from cron import executions
    from cron.install_capability import CapabilityUnavailable, runtime_identity
    from types import ModuleType
    replacement = ModuleType("cron.executions")
    replacement.__dict__.update(vars(executions))
    monkeypatch.setitem(sys.modules, "cron.executions", replacement)
    with pytest.raises(CapabilityUnavailable, match="dynamic import module bindings differ"):
        runtime_identity()


def test_alias_directory_and_entry_symlink_use_the_canonical_writer_lock(tmp_path):
    from pm.plugin_target_lock import target_writer_lock

    home = tmp_path / "home"
    target = home / "plugins/cron-supervisor-report-budget"
    target.mkdir(parents=True)
    (target / "__init__.py").write_text("old\n")
    alias = home / "plugins/alias"
    alias.symlink_to(target, target_is_directory=True)
    env = child_env(home)
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from pm.plugin_target_lock import p1_target_file_writer\n"
        "with p1_target_file_writer(Path(sys.argv[1])):\n"
        "    pass\n"
    )
    with target_writer_lock(target):
        for path in (alias / "__init__.py", target / "__init__.py"):
            result = subprocess.run([sys.executable, "-B", "-c", program, str(path)],
                                    env=env, capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr


def test_forked_child_context_exit_does_not_unlock_the_parent(tmp_path):
    from pm.plugin_target_lock import target_writer_lock

    home = tmp_path / "home"
    target = home / "plugins/cron-supervisor-report-budget"
    target.mkdir(parents=True)
    env = child_env(home)
    program = (
        "from pathlib import Path\n"
        "import sys\n"
        "from pm.plugin_target_lock import target_writer_lock\n"
        "with target_writer_lock(Path(sys.argv[1])):\n"
        "    pass\n"
    )

    context = target_writer_lock(target)
    context.__enter__()
    try:
        child = os.fork()
        if child == 0:
            context.__exit__(None, None, None)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        result = subprocess.run([sys.executable, "-B", "-c", program, str(target)],
                                env=env, capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "BlockingIOError" in result.stderr
    finally:
        context.__exit__(None, None, None)


def test_claim_identity_changes_when_only_its_helper_lock_globals_change(tmp_path, monkeypatch):
    import types
    from contextlib import contextmanager
    from cron import jobs
    from cron.install_capability import runtime_identity

    baseline = runtime_identity(home=tmp_path)
    claim = jobs.claim_job_for_fire
    fence = jobs._under_fire_fence
    with_job = jobs._with_job
    foreign_gate = lambda *args, **kwargs: None
    foreign_jobs_lock_impl = types.FunctionType(
        jobs._jobs_lock.__wrapped__.__code__,
        {**jobs._jobs_lock.__wrapped__.__globals__, "install_gate": foreign_gate},
        jobs._jobs_lock.__wrapped__.__name__, jobs._jobs_lock.__wrapped__.__defaults__,
        jobs._jobs_lock.__wrapped__.__closure__)
    foreign_jobs_lock = contextmanager(foreign_jobs_lock_impl)
    foreign_with_job = types.FunctionType(
        with_job.__code__, {**with_job.__globals__, "_jobs_lock": foreign_jobs_lock},
        with_job.__name__, with_job.__defaults__, with_job.__closure__)
    foreign_fence = types.FunctionType(
        fence.__code__, {**fence.__globals__, "_fire_job_lock": jobs._fire_job_lock},
        fence.__name__, fence.__defaults__, fence.__closure__)
    foreign_claim = types.FunctionType(
        claim.__code__, {**claim.__globals__, "_under_fire_fence": foreign_fence,
                         "_with_job": foreign_with_job},
        claim.__name__, claim.__defaults__, claim.__closure__)
    foreign_claim.__kwdefaults__ = claim.__kwdefaults__
    monkeypatch.setattr(jobs, "claim_job_for_fire", foreign_claim)
    assert foreign_claim.__code__ is claim.__code__
    assert foreign_claim.__kwdefaults__ == claim.__kwdefaults__
    assert runtime_identity(home=tmp_path) != baseline


def test_scheduler_claim_alias_proves_its_own_helper_lock_chain(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from inspect import unwrap
    import types
    from cron import jobs, scheduler
    from cron.install_capability import runtime_identity
    from gateway import control_socket

    def clone(function, bindings):
        result = types.FunctionType(function.__code__, {**function.__globals__, **bindings},
                                    function.__name__, function.__defaults__, function.__closure__)
        result.__kwdefaults__ = function.__kwdefaults__
        return result

    baseline = runtime_identity(home=tmp_path)
    claim = scheduler.claim_job_for_fire
    safe_alias = clone(claim, {})
    monkeypatch.setattr(scheduler, "claim_job_for_fire", safe_alias)
    assert runtime_identity(home=tmp_path) == baseline
    installer = load_installer()
    owner = {"cron_install_gate": control_socket.CRON_INSTALL_GATE_VERSION,
             "cron_install_capability": baseline, "hermes_home": str(tmp_path),
             "pid": os.getpid(), "start_time": 1, "code_sha": "fixture"}
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: owner)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _: 1)
    assert installer.gateway_owner(tmp_path) == tmp_path
    foreign_lock = contextmanager(clone(unwrap(jobs._jobs_lock),
                                       {"install_gate": lambda *a, **k: None}))
    foreign_with_job = clone(jobs._with_job, {"_jobs_lock": foreign_lock})
    foreign_alias = clone(claim, {"_with_job": foreign_with_job})
    monkeypatch.setattr(scheduler, "claim_job_for_fire", foreign_alias)
    assert jobs.claim_job_for_fire is claim
    assert foreign_alias.__code__ is claim.__code__
    assert foreign_alias.__defaults__ == claim.__defaults__
    assert foreign_alias.__kwdefaults__ == claim.__kwdefaults__
    assert runtime_identity(home=tmp_path) != baseline
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        installer.gateway_owner(tmp_path)
