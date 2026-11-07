"""Authentic finish_result -> process exit -> fresh publisher consumption."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from tests.gateway import test_raft_strict_native_transport as transport


async def process(home, mode, endpoint):
    root = Path(__file__).resolve().parents[2]
    fixture = root / 'tests/gateway/raft_strict_native_process.py'
    home.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(home / 'user'), HERMES_HOME=str(home), PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')
    child = await asyncio.create_subprocess_exec(sys.executable, str(fixture), mode, str(home), endpoint,
        env=env, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=40)
    expected = {'produce-exit':17, 'claim-exit':18}.get(mode, 0)
    (home / (mode + '.native.json')).write_text(json.dumps({'native_exit':child.returncode,
        'expected_exit':expected, 'stdout':stdout.decode(), 'stderr':stderr.decode()}))
    assert child.returncode == expected, stdout.decode() + stderr.decode()
    return json.loads(stdout.decode().splitlines()[-1]) if expected == 0 else None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sent', 'drop', '503', 'wrong_target'])
async def test_real_finish_result_exit_fresh_consumers_never_rerun_producer(mode, tmp_path):
    async with transport.peer(mode) as wire:
        await process(tmp_path, 'produce-exit', wire.url)
        assert wire.events == []
        first = await process(tmp_path, 'recover-one', wire.url)
        second = await process(tmp_path, 'recover-two', wire.url)
        part = first['record']['parts']['text']
        assert part['state'] == ('delivered' if mode == 'sent' else 'unknown')
        assert part['attempts'] == 1
        assert first['producer_count'] == second['producer_count'] == 1
        assert first['record']['binding'] == second['record']['binding']
        assert second['record']['parts']['text']['state'] == part['state']
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_claim_owner_exit_without_first_byte_remains_unknown(tmp_path):
    async with transport.peer() as wire:
        await process(tmp_path, 'produce-exit', wire.url)
        await process(tmp_path, 'claim-exit', wire.url)
        first = await process(tmp_path, 'recover-one', wire.url)
        second = await process(tmp_path, 'recover-two', wire.url)
        assert first['record']['parts']['text']['state'] == 'unknown'
        assert first['producer_count'] == second['producer_count'] == 1
        assert wire.events == []
