"""A Future cannot authorize finalization under a replacement task identity."""
from concurrent.futures import Future
import io
import json
import time

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.a2a import protocol, security
from plugins.platforms.a2a.adapter import A2AAdapter


@pytest.mark.parametrize('field', ['context_id', 'peer', 'agent_slug', 'tenant', 'profile_home', 'input_id', 'input_hash'])
@pytest.mark.parametrize('method', ['send', 'stream'])
def test_first_reply_rejects_original_task_identity_swap(monkeypatch, field, method):
    adapter = A2AAdapter(PlatformConfig(enabled=True))
    adapter.tasks = protocol.TaskStore()
    original = adapter.tasks.create('one-task', 'one-context', 'one-peer')
    future = Future()
    future.set_result((protocol.STATE_COMPLETED, 'PRIVATE_OLD_REPLY'))
    pending = {'task_id': 'one-task', 'context_id': 'one-context', 'peer': 'one-peer',
               'future': future, 'created_iso': original['created_iso'], 'started': time.time(), 'record': dict(original)}
    monkeypatch.setattr(adapter, '_prepare_task', lambda *args, **kwargs: (None, pending))
    # Replace the visible record with another currently valid scope; the saved ingress
    # identity must still govern the resolved Future.
    adapter.tasks._tasks['one-task'][field] = 'different-identity'
    effects = []
    monkeypatch.setattr(protocol, 'persist_message', lambda *args, **kwargs: effects.append(('persist', args)))
    monkeypatch.setattr(security, 'audit', lambda *args, **kwargs: effects.append(('audit', args)))
    monkeypatch.setattr(adapter, '_send_push_notification', lambda *args, **kwargs: effects.append(('push', args)))
    before = dict(adapter.tasks._tasks['one-task'])

    class Handler:
        wfile = io.BytesIO()

        def send_response(self, _status):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    if method == 'send':
        response = adapter._rpc_message_send('one-rpc', {}, 'one-peer')
    else:
        handler = Handler()
        adapter._rpc_message_stream(handler, 'one-rpc', {}, 'one-peer')
        wire = handler.wfile.getvalue().decode()
        assert ': done' in wire
        assert 'PRIVATE_OLD_REPLY' not in wire
        response = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith('data: ')][-1]
    assert 'error' in response and 'result' not in response, response
    assert response['error']['code'] == protocol.ERR_TASK_NOT_FOUND
    assert 'PRIVATE_OLD_REPLY' not in json.dumps(response)
    assert adapter.tasks._tasks['one-task'] == before
    assert effects == []
