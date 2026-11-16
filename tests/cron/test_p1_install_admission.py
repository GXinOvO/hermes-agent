"""P1 producer -> consumer admission boundary regression.

The installer owns an exclusive install gate.  The real CronScheduler claim entry
must acquire the corresponding shared gate before it creates an execution row or
claims the job; an installer-held jobs.lock is not authoritative because jobs.py
can degrade after its timeout.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

pytestmark = pytest.mark.platforms("posix")


def test_real_claim_entry_is_blocked_before_execution_creation(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    cron_tmp = tmp_path / "tmp"
    cron_tmp.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(cron_tmp))

    from cron import executions, jobs
    from cron.install_gate import install_gate


    job = jobs.create_job(prompt="p1 admission tracer", schedule="every 1h", deliver="local")
    assert executions.list_executions(job_id=job["id"]) == []
    repo = Path(__file__).resolve().parents[2]
    child = """
import json
from cron import executions
from cron.scheduler_provider import InProcessCronScheduler
result = InProcessCronScheduler().claim_fire(__import__('sys').argv[1], manual=True)
print(json.dumps({'claimed': result is not None,
                  'executions': executions.list_executions(job_id=__import__('sys').argv[1])}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo)

    with install_gate(home, exclusive=True, timeout=1.0, allow_marker=True):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", child, job["id"]],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip())
    assert observed["claimed"] is False
    assert observed["executions"] == []
    assert executions.list_executions(job_id=job["id"]) == []
