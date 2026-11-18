import asyncio
from contextlib import contextmanager
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
async def test_foreign_source_update_between_final_check_and_primary_commit_is_rejected(linked, monkeypatch):
    from agent import oneshot
    from gateway import session_task_sources
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import profiles
    from hermes_state_runtime import begin_runtime_epoch
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope, store_location
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_seed import append_message

    home = linked.home / 'profiles' / 'other'
    home.mkdir(parents=True)
    store = SessionStore(home / 'sessions', GatewayConfig())
    db = open_persistence(profile_scope(home=home), eager=True)
    store._db = db
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='other-owner', persistence=db,
                                 epoch=begin_runtime_epoch(db, instance_id='other-owner'))
    registry = SessionAuthorities(linked.home, multiplexed=True)
    registry.add(linked.home, linked.authority)
    registry.add(home, authority, name='other')
    linked.authority.runner.session_authorities = registry
    runner.session_authorities = registry
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home if name == 'other' else linked.home)
    connection = AuthorityConnection(linked.authority, linked.connection.transport,
                                     {'user_id': linked.connection.actor.subject, 'provider': 'local', 'profile_scope': 'host',
                                      'profile_id': linked.authority.profile_id, 'instance_id': linked.authority.instance_id,
                                      'capabilities': list(linked.connection.actor.capabilities)})
    db.sessions.create(NewSession('shared-race', source='subagent'))
    append_message(db, 'shared-race', 'user', 'Remote requirement before analysis')
    fixtures.seed_read(linked, 'shared-race', profile='other')
    linked.connection = connection
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    original = session_task_sources.SourceRead.check
    injected = []

    probe = '''import sqlite3,sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                       ("shared-race", "user", "Remote requirement changed before primary commit", 1))
    connection.commit()
    print("committed", flush=True)
except sqlite3.OperationalError as exc:
    print("blocked" if "locked" in str(exc).lower() else f"error:{exc}", flush=True)
finally:
    connection.close()
'''
    def race(read):
        original(read)
        if read.connection.authority is authority and raw_holder(linked.db)._conn.in_transaction and not injected:
            child = subprocess.run([sys.executable, '-c', probe, str(store_location(db.scope))],
                                   capture_output=True, text=True, timeout=5, check=True)
            injected.append(child.stdout.strip())

    monkeypatch.setattr(session_task_sources.SourceRead, 'check', race)
    try:
        reply = await fixtures.run_linked(linked, refresh=True)
        assert injected == ['blocked'], {'observed': injected, 'reply': reply}
        assert 'result' in reply, reply
        append_message(db, 'shared-race', 'user', 'Remote update after the primary commit')
        stale = await fixtures.run_linked(linked)
        assert stale['result']['analysis']['stale'] is True

        locked = await fixtures.run_linked(linked, refresh=True)
        assert 'result' in locked, locked
        before = linked.db.state_meta.list_prefix('gateway.task_analysis.v2:')
        timeout_before = raw_holder(db)._conn.execute('PRAGMA busy_timeout').fetchone()[0]
        hold_script = '''import sqlite3,sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
print("ready", flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
'''
        holder = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-c', hold_script, str(store_location(db.scope)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert holder.stdin is not None and holder.stdout is not None
        try:
            ready = await asyncio.wait_for(holder.stdout.readline(), 5)
            assert ready.strip() == b'ready'
            before_fence = threading.Event()
            fence_started = []
            original_fence = session_task_sources.commit_fence

            @contextmanager
            def observed_fence(graph, primary):
                fence_started.append(time.monotonic())
                before_fence.set()
                with original_fence(graph, primary):
                    yield

            monkeypatch.setattr(session_task_sources, 'commit_fence', observed_fence)
            pending = asyncio.create_task(fixtures.run_linked(linked, refresh=True))
            assert await asyncio.wait_for(asyncio.to_thread(before_fence.wait, 5), 6)
            delay = time.monotonic() - fence_started[0]
            assert delay < 0.5, f'Gateway event loop waited {delay:.2f}s for the foreign database lock'
            refused = await asyncio.wait_for(pending, 5)
            assert refused['error']['data']['reason'] == 'storage_unavailable', refused
            assert linked.db.state_meta.list_prefix('gateway.task_analysis.v2:') == before
            assert raw_holder(db)._conn.execute('PRAGMA busy_timeout').fetchone()[0] == timeout_before
            assert not raw_holder(db)._conn.in_transaction
        finally:
            if holder.returncode is None:
                holder.stdin.write(b'\n')
                await holder.stdin.drain()
                await asyncio.wait_for(holder.wait(), 5)
            holder.stdin.close()
            await holder.stdin.wait_closed()
        recovered = await fixtures.run_linked(linked, refresh=True)
        assert 'result' in recovered, recovered
        from gateway.session_task_sources import commit_fence
        from hermes_state_runtime import RuntimeStoreError
        read = SimpleNamespace(connection=SimpleNamespace(authority=authority))
        timeout_before = raw_holder(db)._conn.execute('PRAGMA busy_timeout').fetchone()[0]
        raw_holder(db)._db_wal_generation_lost = True
        try:
            with pytest.raises(RuntimeStoreError) as stopped:
                with commit_fence({'reads': [read]}, linked.db):
                    pytest.fail('retired WAL generation was accepted')
            assert stopped.value.reason == 'session_owner_changed'
            assert raw_holder(db)._conn.execute('PRAGMA busy_timeout').fetchone()[0] == timeout_before
            assert not raw_holder(db)._conn.in_transaction
        finally:
            raw_holder(db)._db_wal_generation_lost = False
    finally:
        await connection.close()
        db.close()
