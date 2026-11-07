import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.session_contract import SessionRef
from gateway.session_events import SessionEvents
from hermes_state import SessionDB
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_side_completion_survives_detach_without_another_model_call(tmp_path):
    from gateway.session_tui_side import _spawn
    from hermes_state_background import begin, results
    from gateway.session_background_results import read_snapshot
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('parent', source='cli')
    authority = SimpleNamespace(db=db, profile_id=str(tmp_path), sessions={'parent':SimpleNamespace(event_stream=SessionEvents())},
                                runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False)))
    ref = SessionRef(str(tmp_path), 'parent')
    identity = {'profile_id':str(tmp_path),'parent_session_id':'parent','event_type':'background.complete','prompt':'task'}
    begin(db, 'bg_one', identity)
    calls = []
    try:
        with patch('gateway.session_runtime_workers.persist_work_count'):
            _spawn(authority, ref, 'bg_one', 'background.complete', lambda:calls.append('run') or 'saved',
                   identity=identity)
            await async_wait_until(lambda:not authority._side_agents, timeout=5, what='side result saved')
        assert len(calls) == 1
        assert results(db, 'parent')[0]['payload'] == {'task_id':'bg_one','text':'saved'}
        received = []
        authority.sessions['parent'].event_stream = SimpleNamespace(publish=lambda sid,payload,**kw:received.append(payload))
        snapshot = [{'event_type':'background.complete','payload':{'task_id':'bg_one','text':'saved'}}]
        assert await read_snapshot(authority, ref) == snapshot
        assert await read_snapshot(authority, ref) == snapshot
        assert not received
        assert calls == ['run']
        assert db.get_messages('parent') == []
    finally:
        db.close()
