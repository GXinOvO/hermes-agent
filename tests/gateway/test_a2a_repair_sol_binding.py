"""Corruption controls using authentic producer records and native fresh recovery."""
from tests.fixtures.storage_holder import raw_holder
import json

import pytest

from tests.gateway.test_a2a_durable_public import seed, recover
from persistence_assembly import open_persistence, profile_scope
from plugins.platforms.a2a.durable_tasks import _key


@pytest.mark.parametrize('mutation', ['peer', 'agent_scope', 'input_id'])
def test_authentic_task_identity_drift_refuses_before_claim(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = seed(tmp_path)
    db = open_persistence(profile_scope(home=tmp_path), eager=True)
    def mutate(conn):
        key = _key(identity['task'])
        record = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
        if mutation == 'peer':
            record['peer'] = 'unrelated-peer'
        elif mutation == 'agent_scope':
            record.update(agent_slug='other', tenant='other')
        else:
            record['input_id'] = 'unrelated-input'
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(record), key))
    raw_holder(db)._execute_write(mutate)
    db.close()
    observed = recover(tmp_path, 'recover')
    print('REVIEW_IDENTITY_DRIFT', json.dumps({'mutation': mutation, 'task': identity, 'result': observed}))
    assert observed['delivered'] == 0, 'task identity drift must not authorize public result publication'
    assert (observed['part']['state'], observed['part']['attempts']) == ('pending', 0)
