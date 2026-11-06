import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_agent import AIAgent
from tools import async_delegation as ad
from tools.registry import registry


@pytest.fixture(autouse=True)
def isolated_ledger():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed():
    ad._persist_dispatch({
        "delegation_id": "durable-query", "parent_session_id": "parent", "session_key": "query-test",
        "dispatched_at": time.time(), "goal": "fixture", "model": "fixture-model",
    })
    ad._persist_completion(
        {"delegation_id": "durable-query", "status": "completed", "completed_at": time.time()},
        {"status": "completed", "summary": "retained result"},
    )


@pytest.mark.parametrize("entry", ["agent", "registry"])
def test_public_dispatch_reads_without_consuming_delivery(entry):
    _seed()
    parent = SimpleNamespace(session_id="parent", _delegate_depth=0)
    args = {"action": "result", "delegation_id": "durable-query"}
    before = ad.get_durable_delegation("durable-query")
    if entry == "agent":
        response = AIAgent._dispatch_delegate_task(parent, args)
    else:
        tool = registry.get_entry("delegate_task")
        assert tool is not None
        response = tool.handler(args, parent_agent=parent)
    result = json.loads(response)
    assert result["result"]["summary"] == "retained result"
    assert result["delivery_state"] == "pending"
    assert ad.get_durable_delegation("durable-query") == before


def test_fresh_process_reads_own_result_and_refuses_foreign_session():
    _seed()
    code = """
import json
from types import SimpleNamespace
from tools.delegate_tool import delegate_task
print(json.dumps([
    json.loads(delegate_task(action='result', delegation_id='durable-query',
                             parent_agent=SimpleNamespace(session_id=owner)))
    for owner in ['parent', 'foreign']
]))
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
    process = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[2],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert process.returncode == 0, process.stderr
    own, foreign = json.loads(process.stdout)
    assert own["result"]["summary"] == "retained result"
    assert own["model"] == "fixture-model"
    assert "error" in foreign and "retained result" not in json.dumps(foreign)
    saved = ad.get_durable_delegation("durable-query")
    assert saved is not None and saved["delivery_state"] == "pending"
