"""A dead producer's sealed Cron journal recovers into a single Bot result consumption."""
import json
import sqlite3

import pytest

from tests.cron.test_completed_result_recovery import _crash_after_result, _recover_in_fresh_reader
from tests.cron.test_sealed_bot_consumer import _wire
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
@pytest.mark.parametrize('window', ['save','queue','mark'])
async def test_dead_producer_recovery_keeps_original_seal_without_running_script_again(sealed_bot, monkeypatch, window):
    import asyncio
    from cron import jobs, scheduler, executions
    from cron.scheduler_authority import journal_path
    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    script = bot.home / 'scripts' / 'report.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("RECOVERED_SEALED_REPORT")\n')
    job = jobs.create_job(prompt=None, script=script.name, no_agent=True, schedule='every 1h', deliver='bot-chat')
    execution_id = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(execution_id)
    # The fixture's child actually executes the script, then exits 73 at the native tail boundary.
    await asyncio.to_thread(_crash_after_result, bot.home, job, window)
    record = json.loads(journal_path(job['id'], execution_id).read_text())
    seal = record['job']['_sealed_bot_result_v1']['seal']
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']
    await asyncio.to_thread(_recover_in_fresh_reader, bot.home)
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        row = db.execute('SELECT status,job_json FROM deliveries WHERE execution_id=?', (execution_id,)).fetchone()
    assert row[0] == 'pending'
    assert json.loads(row[1])['_sealed_bot_result_v1']['seal'] == seal
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    await asyncio.to_thread(_recover_in_fresh_reader, bot.home)
    # The consumer's acceptance settles asynchronously: the next pass books that settlement
    # instead of re-admitting, and only the pass after it finds nothing left to do.
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        assert db.execute('SELECT status FROM deliveries WHERE execution_id=?', (execution_id,)).fetchone()[0] == 'delivered'
    assert len(_admissions(bot)) == len(payloads) == 1
    descriptor = _admissions(bot)[0]['payload']['local_automation_v1']
    assert descriptor['result_recovery_v1'] == 'consume_only'
    assert descriptor['sealed_cron_result_v1']['seal'] == seal
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']
