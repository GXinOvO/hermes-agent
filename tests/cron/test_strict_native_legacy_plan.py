import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from tests.cron.test_strict_mixed_fanout import enqueue, owner, peer


@pytest.mark.asyncio
async def test_saved_legacy_telegram_kind_resumes_without_schema_reselection(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    enqueue(tmp_path, monkeypatch,
            deliver='origin,telegram:555', native_telegram=True)
    old_fixture = Path(__file__).with_name('strict_native_legacy_fixture.py')
    assert old_fixture.is_file()
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    scratch = tmp_path / 'legacy-owner-tmp'
    scratch.mkdir()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(tmp_path),
               TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE='1')
    async with peer() as (url, events):
        process = await asyncio.create_subprocess_exec(sys.executable, '-B', str(old_fixture),
            str(tmp_path), url, 'after:0:done', env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 45)
        print('LEGACY_NATIVE_OWNER ' + json.dumps({'pid': process.pid, 'exit': process.returncode,
                                                 'stdout': out.decode(), 'stderr': err.decode()}))
        assert process.returncode == 88, err.decode()
        saved = json.loads(queue.get_status('fanout-exec')['thread_json'])
        assert [target['kind'] for target in saved['binding']['targets']] == ['thread', 'telegram']
        assert saved['items'][0]['stage'] == 'done' and saved['items'][1]['stage'] == 'initial'
        assert len(events) == 2
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    assert first['row']['status'] == second['row']['status'] == 'delivered'
    assert len(events) == 3 and events[0]['path'] == '/chat.postMessage' and events[2]['path'].endswith('/sendMessage')
    audit = json.loads(second['row']['thread_receipts_json'])
    assert [item['kind'] for item in audit['items']] == ['thread', 'telegram']
