import asyncio
from contextlib import closing
from datetime import datetime, timedelta, UTC
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from types import SimpleNamespace
import uuid

import pytest

from tests.cron.test_p1_install_recovery import load_installer

pytestmark = pytest.mark.platforms("posix")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/p1_install"


@pytest.mark.parametrize("before_file,after_file", [
    ("old-plugin.py.fixture", "r3-plugin.py.fixture"),
    ("r3-plugin.py.fixture", "r2-plugin.py.fixture")])
def test_real_old_r3_hook_activation_and_interrupted_recovery(monkeypatch, tmp_path, before_file, after_file):
    async def scenario(root):
        from hermes_cli import plugins
        from gateway import run_plugin_rewire

        from cron.execution_identity import CronExecution, _CURRENT
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        home = root / "profiles/cron-supervisor"
        target = home / "plugins/cron-supervisor-report-budget/__init__.py"
        target.parent.mkdir(parents=True)
        cron = home / "cron"
        (cron / "admissions").mkdir(parents=True)
        (home / "config.yaml").write_text("plugins:\n  enabled: [cron-supervisor-report-budget]\n  disabled: []\n")
        target.with_name("plugin.yaml").write_text("name: cron-supervisor-report-budget\nversion: 0.1.0\n")
        old, new = (FIXTURES / before_file).read_bytes(), (FIXTURES / after_file).read_bytes()
        sha = lambda data: hashlib.sha256(data).hexdigest()
        target.write_bytes(old)
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(root / "no-bundled"))
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
        monkeypatch.setattr("gateway.status._get_code_identity_fields",
                            lambda: {"code_sha": "fixture-code", "code_version": "fixture"})
        from gateway import control_socket
        from cron import scheduler_provider as sp
        import threading
        provider = sp.resolve_cron_scheduler()
        stop = threading.Event()
        stop.set()
        # Exercise the real import/capture path without dispatching a tick or starting a daemon.
        provider.start(stop)
        monkeypatch.setattr(sp, "_install_runtime_provider", provider)
        job = {"id": "341bad758066", "enabled": True, "state": "scheduled",
               "next_run_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
        (cron / "jobs.json").write_text(json.dumps({"jobs": [job]}))
        with closing(sqlite3.connect(cron / "executions.db")) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT)")
        key = f"{cron.resolve()}::{job['id']}"
        for path in (cron / ".tick.lock", cron / ".jobs.lock",
                     cron / f".fire-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.lock"):
            path.touch()
        plugins._reset_plugin_managers_for_tests()
        plugins.discover_plugins()
        token = _CURRENT.set(CronExecution(job_id=job["id"], job_name="fixture", execution_id="a"*32,
                             source="fixture", scheduled_instant=None, started_at=None, profile="cron-supervisor"))
        home_token = set_hermes_home_override(home)
        text = "覆盖：对象索引未列未知，报告未覆盖异常未知。\n\nCRON_SUPERVISOR_V1 status=read_only"
        invoke = lambda: plugins.invoke_hook("transform_llm_output", response_text=text, platform="cron")
        monkeypatch.setattr(run_plugin_rewire, "_reconcile_plugin_mcp_servers", lambda *a: None)
        runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False), adapters={})
        reload_handler = run_plugin_rewire.reload_plugins_verb(runner, asyncio.get_running_loop())
        queries = []
        def query(requested, verb, **kwargs):
            assert Path(requested) == home
            queries.append(verb)
            return (control_socket.build_identify_payload() if verb == "identify"
                    else reload_handler(kwargs.get("params")))
        monkeypatch.setattr(control_socket, "query_gateway_control", query)
        installer = load_installer()
        arguments = dict(base=home, candidate=FIXTURES / after_file, receipt_dir=root / "receipts",
                         old=sha(old), new=sha(new))
        try:
            before_outputs = invoke()
            assert len(before_outputs) == (0 if before_file == "old-plugin.py.fixture" else 1)
            result = await asyncio.to_thread(installer.apply, **arguments)
            assert result["status"] == "applied"
            assert result["activation"]["loaded_sources"]["cron-supervisor-report-budget"] == sha(new)
            outputs = invoke()
            assert len(outputs) == 1 and "本次快照未核验" in outputs[0]
            assert len(outputs[0]) <= 700
            assert not (cron / ".install-pending.json").exists()
            assert queries.count("identify") >= 3 and queries.count("reload-plugins") == 1
            installer.publish(target, old, 0o644, expected=sha(new))
            plugins.discover_plugins(force=True)
            assert invoke() == before_outputs
            calls = []
            original_reload = installer.reload_plugin
            def timeout_after_publish(*_):
                calls.append(True)
                raise installer.ActivationUnconfirmed("injected reload timeout")
            installer.__dict__["reload_plugin"] = timeout_after_publish
            with pytest.raises(installer.ActivationUnconfirmed):
                await asyncio.to_thread(installer.apply, **arguments)
            installer.__dict__["reload_plugin"] = original_reload
            assert calls == [True] and sha(target.read_bytes()) == sha(new)
            assert (cron / ".install-pending.json").exists()
            assert invoke() == before_outputs
            recovered = await asyncio.to_thread(installer.reconcile, home)
            assert recovered["status"] == "reconciled" and sha(target.read_bytes()) == sha(old)
            assert invoke() == before_outputs
            assert not (cron / ".install-pending.json").exists()
        finally:

            reset_hermes_home_override(home_token)
            _CURRENT.reset(token)
            plugins._reset_plugin_managers_for_tests()

    asyncio.run(scenario(tmp_path))


def test_normal_reload_keeps_home_only_compatibility_and_marker_gate(monkeypatch, tmp_path):
    async def scenario():
        from hermes_cli import plugins
        from hermes_cli import plugins_activation
        from gateway import run_plugin_rewire

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        manager = SimpleNamespace(_plugins={})
        monkeypatch.setattr(plugins, "discover_plugins", lambda force=False: None)
        monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
        monkeypatch.setattr(plugins_activation, "activation_summaries", lambda _manager: [])
        monkeypatch.setattr(plugins_activation, "dependency_blocked_summaries", lambda _manager: [])
        monkeypatch.setattr(run_plugin_rewire, "_reconcile_plugin_mcp_servers", lambda *args: None)
        runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False), adapters={})
        handler = run_plugin_rewire.reload_plugins_verb(runner, asyncio.get_running_loop())
        normal = await asyncio.to_thread(handler, {"home": str(home)})
        assert normal["reloaded"] is True and normal["home"] == str(home)
        wrong = await asyncio.to_thread(handler, {"home": str(tmp_path / "wrong")})
        assert wrong["reloaded"] is False
        marker = home / "cron/.install-pending.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"token":"expected"}')
        missing_token = await asyncio.to_thread(handler, {"home": str(home)})
        assert missing_token["reloaded"] is False
        assert "reconciliation" in missing_token["error"]
        import copy
        import hashlib
        from gateway import control_socket
        owner = {"hermes_home": str(home), "pid": os.getpid(), "start_time": 1,
                 "code_sha": "fixture", "cron_install_gate": control_socket.CRON_INSTALL_GATE_VERSION,
                 "cron_install_capability": {"version": 7, "bindings": {"gate": "before"}}}
        monkeypatch.setattr(control_socket, "build_identify_payload", lambda: owner)
        fingerprint = lambda: hashlib.sha256(json.dumps(owner, sort_keys=True, separators=(",", ":"),
                                                        ensure_ascii=True).encode()).hexdigest()
        proof = {"home": str(home), "install_token": "expected", "owner_sha256": fingerprint()}
        accepted = await asyncio.to_thread(handler, proof)
        assert accepted["reloaded"] is True and accepted["owner"] == owner
        legacy = await asyncio.to_thread(handler, {"home": str(home), "install_token": "expected", "owner": owner})
        assert legacy["reloaded"] is False
        for key, value in list(owner.items()):
            saved = copy.deepcopy(value)
            owner[key] = {"changed": True} if isinstance(saved, dict) else "changed"
            refused = await asyncio.to_thread(handler, proof)
            assert refused["reloaded"] is False and "owner changed" in refused["error"]
            owner[key] = saved

    asyncio.run(scenario())
