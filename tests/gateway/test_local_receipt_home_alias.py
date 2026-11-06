import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


@asynccontextmanager
async def _moved_home(tmp_path, monkeypatch, profile, alias='valid'):
    from gateway import run
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from hermes_state_local import POLICY_PREFIX

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    old, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha', 'beta'))
    runners = []
    reserved = []

    def close(runner):
        from gateway.session_cron import unbind_owner
        for authority in getattr(runner, 'session_authorities', ()):
            unbind_owner(authority)
            authority.db.close()
        for db in runner.session_store._db_handles.values():
            db.close()

    try:
        process_ownership.reserve([home for _, home in homes])
        reserved = list(homes)
        first = _runner(old, homes)
        runners.append(first)
        await initialize_gateway_runtime(first)
        home = dict(homes)[profile]
        authority = first.session_authorities.for_home(home)
        actor = SessionActor('alias-owner', authority.profile_id,
                            frozenset({'session:create', 'session:read', 'session:submit'}), 'socket')
        with _profile_runtime_scope(home, {}):
            original = create_local_session(authority, actor, {
                'request_id': 'before-move', 'source': 'gui', 'cwd': str(tmp_path),
                'model': 'fixture', 'toolsets': []})
        with authority.db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (POLICY_PREFIX + original.session_id,)).fetchone()[0]
        close(first)
        for _, owned in reserved:
            process_ownership.release(owned)
        reserved = []
        new = tmp_path / 'moved' / '.hermes'
        new.parent.mkdir()
        old.rename(new)
        if alias == 'valid':
            old.symlink_to(new, target_is_directory=True)
        elif alias == 'foreign':
            foreign = tmp_path / 'foreign'
            foreign.mkdir()
            old.symlink_to(foreign, target_is_directory=True)
        moved = [(name, new / path.relative_to(old)) for name, path in homes]
        monkeypatch.setenv('HERMES_HOME', str(new))
        process_ownership.reserve([path for _, path in moved])
        reserved = moved
        second = _runner(new, moved)
        runners.append(second)
        await initialize_gateway_runtime(second)
        authority = second.session_authorities.for_home(dict(moved)[profile])
        yield second, authority, original, raw, old, new, moved
    finally:
        for runner in reversed(runners):
            close(runner)
        for _, home in reserved:
            process_ownership.release(home)


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['default', 'alpha'])
async def test_native_home_alias_cold_restore_and_automation_note(tmp_path, monkeypatch, profile):
    from gateway.run import _profile_runtime_scope
    from gateway.session_contract import SessionRef
    from gateway.session_cron import _admit_local_delivery
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_keys import local_identity
    from hermes_state_local import POLICY_PREFIX, local_receipt
    from hermes_state_runtime import list_session_admissions

    async with _moved_home(tmp_path, monkeypatch, profile) as moved:
        runner, authority, original, raw, _old, _new, homes = moved
        receipt = json.loads(raw)
        assert original.profile_id != authority.profile_id
        assert receipt['profile_id'] == original.profile_id
        assert original.session_id == local_identity(receipt['profile_id'], receipt['principal_id'],
                                                     receipt['request_id'])
        assert original.session_id in authority.sessions, 'owned native receipt was paused after home move'
        with _profile_runtime_scope(dict(homes)[profile], {}):
            ref = restore_local_session(authority, original.session_id)
        assert ref == SessionRef(authority.profile_id, original.session_id)
        live = authority.sessions[ref.session_id]
        assert live.route == receipt['route']
        assert live.source.user_id == receipt['principal_id']
        assert live.source.profile == (None if profile == 'default' else profile)
        assert live.source.profile == receipt['entry']['origin'].get('profile')

        async def no_turn(event):
            raise AssertionError('a cron completion must be a note without a model call')
        runner._handle_message = no_turn
        assert await _admit_local_delivery(authority, ref.session_id, 'ALIAS_NATIVE_NOTE', {})
        await authority._drain(ref)
        notes = [m for m in authority.db.get_messages(ref.session_id)
                 if m.get('content') == 'ALIAS_NATIVE_NOTE']
        assert len(notes) == 1
        assert notes[0]['display_kind'] == 'internal_notification'
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert len(rows) == 1 and rows[0]['outcome'] == 'completed'
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?',
                                (POLICY_PREFIX + ref.session_id,)).fetchone()[0] == raw
        assert local_receipt(authority.db, ref.session_id) == receipt
        for name, home in homes:
            if name != profile:
                assert runner.session_authorities.for_home(home).db.get_session(ref.session_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['default', 'alpha'])
async def test_alias_note_fifo_and_retry_keep_original_owner(tmp_path, monkeypatch, profile):
    from gateway.platforms.event import MessageEvent
    from gateway.session_authorities import owner_scope
    from gateway.session_contract import SessionRef
    from gateway.session_cron import _admit_local_delivery
    from hermes_state_local import local_receipt
    from hermes_state_runtime import admit_session_input, list_session_admissions

    async with _moved_home(tmp_path, monkeypatch, profile) as moved:
        runner, authority, original, raw, _old, _new, _homes = moved
        ref = SessionRef(authority.profile_id, original.session_id)
        assert ref.session_id in authority.sessions
        live = authority.sessions[ref.session_id]
        seen = []

        def notes():
            return [m for m in authority.db.get_messages(ref.session_id)
                    if m.get('content') == 'ALIAS_FIFO_NOTE']

        async def turn(event):
            seen.append((event.text, len(notes())))
            return 'ok'
        runner._handle_message = turn
        admit_session_input(authority.db, epoch=authority.epoch, principal_id='alias-owner',
                            session_id=ref.session_id, request_id='before', payload={'text': 'BEFORE'})
        assert await _admit_local_delivery(authority, ref.session_id, 'ALIAS_FIFO_NOTE', {})
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        identity = rows[1]['request_id']
        assert rows[1]['payload']['local_automation_v1']['owner'] == original.session_id
        assert notes() == []
        admit_session_input(authority.db, epoch=authority.epoch, principal_id='alias-owner',
                            session_id=ref.session_id, request_id='after', payload={'text': 'AFTER'})
        await authority._drain(ref)
        assert seen == [('BEFORE', 0), ('AFTER', 1)]
        assert len(notes()) == 1

        with owner_scope(authority):
            event = MessageEvent(text='ALIAS_FIFO_NOTE', source=live.source, internal=True, metadata={
                'gateway_session_key': live.route, 'gateway_session_id': ref.session_id})
            await authority.admit_automation(runner._adapter_for_source(live.source), event, identity)
        await authority._drain(ref)
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert len(rows) == 3 and [r['outcome'] for r in rows] == ['completed'] * 3
        assert len(notes()) == 1 and seen == [('BEFORE', 0), ('AFTER', 1)]
        assert local_receipt(authority.db, ref.session_id) == json.loads(raw)


def _damage_alias(old, other, tmp_path, db_path, variant):
    old.unlink()
    if variant in {'retarget', 'retarget_hardlinked_db'}:
        target = other / 'profiles' / 'alpha'
        target.mkdir(parents=True)
        if variant == 'retarget_hardlinked_db':
            os.link(db_path, target / 'state.db')
            assert (target / 'state.db').samefile(db_path)
    target = {'missing_alias': None, 'dangling_alias': tmp_path / 'absent', 'alias_loop': old,
              'retarget': other, 'retarget_hardlinked_db': other}[variant]
    if target is not None:
        old.symlink_to(target, target_is_directory=True)


def _hide_database(db_path, symlink=False):
    saved = db_path.with_suffix('.saved')
    db_path.rename(saved)
    if symlink:
        db_path.symlink_to(saved)
    def undo():
        if symlink:
            db_path.unlink()
        saved.rename(db_path)
    return undo


def _use_foreign_database(authority, other):
    from hermes_state import SessionDB
    foreign = SessionDB(other / 'state.db')
    original_path = authority.db.db_path
    authority.db.db_path = foreign.db_path
    def undo():
        authority.db.db_path = original_path
        foreign.close()
    return undo


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['missing_alias', 'dangling_alias', 'alias_loop', 'retarget',
                                    'retarget_hardlinked_db', 'missing_db', 'missing_owner',
                                    'other_home', 'other_db', 'db_symlink', 'foreign_actor', 'foreign_ref'])
async def test_native_alias_rejects_foreign_or_changed_paths(tmp_path, monkeypatch, variant):
    from gateway.session_contract import SessionActor, SessionRef
    from gateway.session_cron import _admit_local_delivery
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_runtime import RuntimeStoreError, list_session_admissions

    async with _moved_home(tmp_path, monkeypatch, 'alpha') as moved:
        _runner, authority, original, _raw, old, _new, homes = moved
        ref = SessionRef(authority.profile_id, original.session_id)
        assert ref.session_id in authority.sessions
        owned_receipt(authority, ref.session_id)
        other = tmp_path / 'other'
        other.mkdir()
        db_path = Path(authority.db.db_path)
        def undo():
            pass
        actions = {
            **{name: lambda: _damage_alias(old, other, tmp_path, db_path, variant)
               for name in ('missing_alias', 'dangling_alias', 'alias_loop', 'retarget', 'retarget_hardlinked_db')},
            'missing_db': lambda: _hide_database(db_path),
            'missing_owner': lambda: setattr(authority, 'profile_id', str(other / 'absent')),
            'other_home': lambda: setattr(authority, 'profile_id', str(dict(homes)['beta'])),
            'other_db': lambda: _use_foreign_database(authority, other),
            'db_symlink': lambda: _hide_database(db_path, symlink=True),
        }
        if variant in actions:
            undo = actions[variant]() or undo
        try:
            if variant in {'foreign_actor', 'foreign_ref'}:
                actor = SessionActor('alias-owner', authority.profile_id,
                                     frozenset({'session:read'}), 'socket')
                if variant == 'foreign_actor':
                    actor = SessionActor(actor.subject, original.profile_id, actor.capabilities, 'socket')
                else:
                    ref = original
                with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
                    await authority.resolve(actor, ref)
            else:
                with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
                    owned_receipt(authority, ref.session_id)
                with pytest.raises(RuntimeStoreError):
                    await _admit_local_delivery(authority, ref.session_id, 'MUST_NOT_WRITE', {})
                assert not list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        finally:
            undo()


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['route', 'entry', 'principal', 'lineage', 'identity', 'origin_profile'])
async def test_alias_keeps_native_receipt_identity_checks(tmp_path, monkeypatch, variant):
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import RuntimeStoreError

    async with _moved_home(tmp_path, monkeypatch, 'alpha') as moved:
        _runner, authority, original, raw, _old, _new, _homes = moved
        receipt = json.loads(raw)
        node, key, value = {
            'route': (receipt, 'route', receipt['route'].replace('agent:alpha:', 'agent:beta:')),
            'entry': (receipt['entry'], 'session_key', receipt['entry']['session_key'] + ':foreign'),
            'principal': (receipt, 'principal_id', 'foreign'),
            'lineage': (receipt, 'lineage', ['unrelated', original.session_id]),
            'identity': (receipt, 'session_id', 'unrelated'),
            'origin_profile': (receipt['entry']['origin'], 'profile', 'beta'),
        }[variant]
        node[key] = value
        changed = json.dumps(receipt)
        authority.db.set_meta(POLICY_PREFIX + original.session_id, changed)
        authority.sessions.pop(original.session_id)
        with pytest.raises(RuntimeStoreError):
            restore_local_session(authority, original.session_id)
        assert original.session_id not in authority.sessions
        assert authority.db.get_meta(POLICY_PREFIX + original.session_id) == changed


@pytest.mark.asyncio
async def test_alias_rejects_unknown_open_database_identity(tmp_path, monkeypatch):
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_runtime import RuntimeStoreError

    async with _moved_home(tmp_path, monkeypatch, 'alpha') as moved:
        _runner, authority, original, _raw, _old, _new, _homes = moved
        monkeypatch.setattr(authority.db, '_db_file_identity', None)
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            owned_receipt(authority, original.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', ['missing', 'foreign'])
async def test_cold_restore_refuses_unproven_alias(tmp_path, monkeypatch, alias):
    from gateway.session_cron import _admit_local_delivery
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import RuntimeStoreError, list_session_admissions

    async with _moved_home(tmp_path, monkeypatch, 'alpha', alias=alias) as moved:
        _runner, authority, original, raw, _old, _new, _homes = moved
        assert original.session_id not in authority.sessions
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            await _admit_local_delivery(authority, original.session_id, 'MUST_NOT_WRITE', {})
        assert not list_session_admissions(authority.db, session_id=original.session_id, pending_only=False)
        assert authority.db.get_meta(POLICY_PREFIX + original.session_id) == raw


@pytest.mark.asyncio
@pytest.mark.parametrize('linked_db', [False, True])
async def test_alias_retarget_during_validation_is_refused(tmp_path, monkeypatch, linked_db):
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_runtime import RuntimeStoreError

    async with _moved_home(tmp_path, monkeypatch, 'alpha') as moved:
        _runner, authority, original, _raw, old, _new, _homes = moved
        other = tmp_path / 'other'
        target = other / 'profiles' / 'alpha'
        target.mkdir(parents=True)
        if linked_db:
            os.link(authority.db.db_path, target / 'state.db')
        samefile = Path.samefile
        changed = False

        def retarget(path, other_path):
            nonlocal changed
            result = samefile(path, other_path)
            if path == Path(original.profile_id) and not changed:
                old.unlink()
                old.symlink_to(other, target_is_directory=True)
                changed = True
            return result

        monkeypatch.setattr(Path, 'samefile', retarget)
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            owned_receipt(authority, original.session_id)
        assert changed


@pytest.mark.asyncio
async def test_alias_rejects_replaced_open_database(tmp_path, monkeypatch):
    from gateway.session_local_recovery import owned_receipt
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError

    async with _moved_home(tmp_path, monkeypatch, 'alpha') as moved:
        _runner, authority, original, _raw, _old, _new, _homes = moved
        db = authority.db
        path = Path(db.db_path)
        saved = path.with_suffix('.saved')
        replacement = tmp_path / 'replacement.db'
        foreign = SessionDB(replacement)
        foreign.create_session('foreign', source='gui')
        foreign.close()
        path.rename(saved)
        replacement.rename(path)
        try:
            assert (Path(original.profile_id) / 'state.db').samefile(path)
            assert db._db_file_was_replaced()
            with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
                owned_receipt(authority, original.session_id)
        finally:
            path.rename(replacement)
            saved.rename(path)


@pytest.mark.asyncio
async def test_fixture_profile_remains_literal_only(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_cron import unbind_owner
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_runtime import RuntimeStoreError

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='literal')
    try:
        actor = SessionActor('owner', 'fixture', frozenset({'session:create'}), 'socket')
        ref = create_local_session(authority, actor, {
            'request_id': 'literal', 'source': 'gui', 'model': 'fixture', 'toolsets': []})
        receipt = owned_receipt(authority, ref.session_id)[0]
        assert receipt['profile_id'] == 'fixture'
        authority.profile_id = 'other-fixture'
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            owned_receipt(authority, ref.session_id)
    finally:
        unbind_owner(authority)
        runner.session_store.close_all_db_handles()
