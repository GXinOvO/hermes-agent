import asyncio
import json
import sqlite3

import pytest

from tests.cron import test_sealed_bot_consumer as fixtures

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['journal', 'pending'])
async def test_sealed_bot_reader_accepts_windows_utf8_bom(sealed_bot, monkeypatch, location):
    from cron import scheduler_results, sealed_bot_result
    if location == 'journal':
        save = scheduler_results._save
        def windows_save(path, record):
            save(path, record)
            path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
        monkeypatch.setattr(scheduler_results, '_save', windows_save)
        await asyncio.to_thread(fixtures._produce, sealed_bot)
        job, row = fixtures._queued_job(sealed_bot)
        assert job['_sealed_bot_result_v1']['result'][2] == 'SEALED_REPORT'
        assert row['content'] == 'SEALED_REPORT'
    else:
        await asyncio.to_thread(fixtures._produce, sealed_bot)
        job, row = fixtures._queued_job(sealed_bot)
        pending = sealed_bot.home / 'cron' / 'bot_chat_pending' / 'fixture.json'
        pending.parent.mkdir(exist_ok=True)
        pending.write_text(json.dumps({'id':'fixture', 'status':'queued', 'job':job,
            'content':row['content'], 'for_failure':row['for_failure']}), encoding='utf-8-sig')
        with sqlite3.connect(sealed_bot.home / 'cron' / 'deliveries.db') as db:
            db.execute("UPDATE deliveries SET job_json='{}' WHERE execution_id=?", (row['execution_id'],))
        recovered = sealed_bot_result._row(sealed_bot.home, row['execution_id'], 'fixture')
        assert json.loads(recovered['job_json']) == job
        assert recovered['content'] == 'SEALED_REPORT'
