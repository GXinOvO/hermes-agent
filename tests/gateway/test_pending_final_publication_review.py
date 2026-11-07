"""Independent bounded API03 contract probes; frozen source is never edited."""
from copy import copy, deepcopy
from contextlib import nullcontext
import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from gateway import pending_final_publication as pub
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport
from hermes_state_terminal import RESULT_PREFIX, load_result
from tests.gateway import test_pending_final as fixtures
from tests.gateway.test_pending_final_original_identity import settle
from tests.gateway.test_pending_final_publication_receipt import prepared_claim, publication


@pytest.mark.asyncio
@pytest.mark.parametrize('raw_kind', ['payload_list', 'native_list', 'source_list', 'automation_list',
                                      'local_automation_list', 'payload_null', 'invalid_json'])
async def test_recovery_bad_shape_keeps_healthy_sibling_and_ordinary_orphan(tmp_path, monkeypatch, raw_kind):
    handles = []
    values = []
    for label in ['bad', 'healthy', 'orphan']:
        db, epoch, row, _event, descriptor, authority = await fixtures._sealed(tmp_path / label, human=True)
        handles.append(db)
        settle(db, epoch, row, descriptor)
        values.append((db, row, descriptor, authority))
    try:
        bad_db, bad_row, bad_value, _ = values[0]
        payload = deepcopy(bad_row['payload'])
        edits = {
            'native_list': (payload, 'native_text_v1'),
            'source_list': (payload['native_text_v1'], 'source'),
            'automation_list': (payload['native_text_v1'], 'automation'),
            'local_automation_list': (payload, 'local_automation_v1'),
        }
        if raw_kind in edits:
            record, key = edits[raw_kind]
            record[key] = []
        elif raw_kind != 'invalid_json':
            payload = {'payload_list': [], 'payload_null': None}[raw_kind]
        raw = '{broken' if raw_kind == 'invalid_json' else json.dumps(payload)
        bad_db._execute_write(lambda c: c.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                                   (raw, bad_row['admission_id'])))
        orphan_db, orphan_row, _, _ = values[2]
        assert pf.claim(orphan_db, orphan_row['admission_id'], 'text', 'sealed final')[1]
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda *args: False)
        monkeypatch.setattr('gateway.session_authorities.owner_scope', lambda *args: nullcontext())
        async def route(runner, payload, session, source, adapter):
            return source, payload['native_text_v1']['route']
        monkeypatch.setattr('gateway.session_envelope.check_native_route', route)
        sent = []
        class Adapter(fixtures.Adapter):
            @single_attempt_transport
            async def send(self, chat_id, content, **kwargs):
                sent.append((chat_id, content))
                return SendResult(success=True, message_id='ordinary-human')
        adapter = Adapter()
        runner = SimpleNamespace(session_authorities=[v[3] for v in values],
            _delivery_adapter_for=lambda source: adapter,
            session_store=SimpleNamespace(lookup_by_session_key=lambda route:
                SimpleNamespace(session_id='sid', suspended=False)))
        assert await pf.recover(runner) == 1
        assert sent == [('chat', 'sealed final')]
        states = [pf.pending(db)[0]['parts']['text'] for db in handles]
        assert [(p['state'], p['attempts']) for p in states] == [('pending', 0), ('delivered', 1), ('unknown', 1)]
        assert all('publication_receipt' not in p for p in states)
        with bad_db._read_ctx() as conn:
            assert conn.execute('SELECT payload_json FROM session_admissions WHERE admission_id=?',
                                (bad_row['admission_id'],)).fetchone()[0] == raw
            assert pf._valid(conn, bad_value, tmp_path / 'bad') is False
        print(json.dumps({'raw_kind': raw_kind, 'delivered': 1, 'states': [(p['state'], p['attempts']) for p in states],
                          'bad_raw_preserved': True, 'ordinary_human_compatibility': True}))
    finally:
        for db in handles:
            db.close()


@pytest.mark.asyncio
async def test_legacy_pending_without_independent_identity_must_not_mint_publication(tmp_path):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path, human=True)
    try:
        payload = deepcopy(row['payload'])
        payload['native_text_v1']['source'].update(platform='a2a', chat_id='context')
        row['payload'] = payload
        db._execute_write(lambda c: c.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                               (json.dumps(payload), row['admission_id'])))
        from gateway.config import Platform
        event.source.platform = Platform('a2a')
        event.source.chat_id = 'context'
        event.reply_anchor_override = 'task'
        descriptor = await pf.prepare(authority, row, event, payload['native_text_v1']['route'], 'sealed final')
        settle(db, epoch, row, descriptor)
        def legacy_shape(conn):
            value = pf._read(conn, row['admission_id'])
            value.pop('identity_witness_version')
            pf._put(conn, row['admission_id'], value)
            key = RESULT_PREFIX + row['admission_id']
            saved = load_result(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
            saved.pop('_pending_final_identity_v1')
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(saved), key))
        db._execute_write(legacy_shape)
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
        value = pf.pending(db)[0]
        with db._read_ctx() as conn:
            assert pf.read_identity_witness(conn, tmp_path, value['binding']) is None
        rejected = False
        try:
            cap, _ = publication(db, row)
        except ValueError:
            rejected = True
        observation = {'capture_rejected': rejected, 'legacy_witness': None}
        if not rejected:
            observation['ack_accepted'] = pf.finish(db, row['admission_id'], 'text',
                SendResult(success=True, message_id='task', raw_response={'publication_receipt_v1': cap}))
            current = pf.pending(db)[0]
            observation['state'] = current['parts']['text']['state']
            observation['receipt_persisted'] = 'publication_receipt' in current['parts']['text']
            try:
                with db._read_ctx() as conn:
                    observation['read'] = pub.read(conn, tmp_path, current['binding'])
            except ValueError as exc:
                observation['read_error'] = str(exc)
        print(json.dumps(observation))
        assert rejected, 'API03 capture minted a capability without its required independent original identity witness'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['null', 'bool_version', 'extra_field', 'hash_uppercase', 'unknown',
                                  'missing_ack_time', 'wrong_platform', 'wrong_context'])
async def test_present_malformed_receipt_is_valueerror_and_readonly(tmp_path, fault):
    db, row, value = await prepared_claim(tmp_path)
    try:
        cap, _ = publication(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                         raw_response={'publication_receipt_v1': cap}))
        def corrupt(conn):
            current = pf._read(conn, row['admission_id'])
            part = current['parts']['text']
            proof = part['publication_receipt']
            edits = {
                'null': [(part, 'publication_receipt', None)],
                'bool_version': [(proof, 'version', True)],
                'extra_field': [(proof, 'extra', 'bad')],
                'hash_uppercase': [(proof, 'public_reply_sha256', 'A' * 64)],
                'unknown': [(part, 'state', 'unknown')],
                'missing_ack_time': [],
                'wrong_platform': [(proof, 'platform', 'telegram')],
                'wrong_context': [(proof, 'context_id', 'other-context')],
            }
            for record, key, value in edits[fault]:
                record[key] = value
            if fault in {'unknown', 'missing_ack_time'}:
                part.pop('delivered_at')
            pf._put(conn, row['admission_id'], current)
        db._execute_write(corrupt)
        with db._read_ctx() as conn:
            key = pf.PREFIX + row['admission_id']
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0]
            changes = conn.total_changes
            error = None
            result = None
            try:
                result = pub.read(conn, tmp_path, value['binding'])
            except ValueError as exc:
                error = str(exc)
            assert conn.total_changes == changes
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0] == raw
        print(json.dumps({'fault': fault, 'result': result, 'error': error, 'readonly': True}))
        assert error is not None, 'a present invalid receipt must not be treated as absent history'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['copy', 'same_row_hash_drift', 'utf8_positive', 'failed_ack'])
async def test_capability_copy_and_current_row_drift_do_not_mint_history(tmp_path, fault):
    db, row, _value = await prepared_claim(tmp_path)
    try:
        public = '公开正文🙂\n' if fault == 'utf8_positive' else 'public final'
        key = 'review.publication:' + row['admission_id']
        def verify(conn, receipt, sealed):
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
            if raw is None:
                return False
            saved = json.loads(raw[0])
            return (saved['reply'] == public and saved['original_content'] == sealed['parts']['text']['content']
                and saved['hash'] == receipt['public_reply_sha256'] == hashlib.sha256(saved['reply'].encode()).hexdigest())
        def publish(conn):
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                (key, json.dumps({'reply': public, 'hash': hashlib.sha256(public.encode()).hexdigest(),
                                  'original_content': 'sealed final'})))
            return pub.capture(conn, tmp_path, row['admission_id'], 'text', task_id='task', context_id='context',
                               public_reply=public, verify=verify)
        cap = db._execute_write(publish)
        if fault == 'same_row_hash_drift':
            changed = {'reply': 'changed', 'hash': hashlib.sha256(b'changed').hexdigest(), 'original_content': 'sealed final'}
            db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(changed), key)))
        candidate = copy(cap) if fault == 'copy' else cap
        accepted = pf.finish(db, row['admission_id'], 'text', SendResult(success=fault != 'failed_ack',
            message_id='task', raw_response={'publication_receipt_v1': candidate}))
        part = pf.pending(db)[0]['parts']['text']
        if fault == 'utf8_positive':
            assert accepted and part['state'] == 'delivered'
            assert part['publication_receipt']['public_reply_sha256'] == hashlib.sha256(public.encode('utf-8')).hexdigest()
            assert part['publication_receipt']['public_reply_sha256'] != pf._digest(public)
        else:
            assert part['state'] == 'unknown' and 'delivered_at' not in part and 'publication_receipt' not in part
        print(json.dumps({'fault': fault, 'accepted': accepted, 'state': part['state'],
                          'receipt_persisted': 'publication_receipt' in part}))
    finally:
        db.close()
