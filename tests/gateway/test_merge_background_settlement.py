from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB
from hermes_state_background import _key, _load, _save
from tests.gateway.test_background_command import _make_runner


@pytest.mark.asyncio
async def test_saved_background_result_claim_failure_is_not_execution_failure(tmp_path):
    task_id = 'claim-drift'
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    with SessionDB(tmp_path / 'state.db') as db:
        runner = _make_runner()
        runner._session_db = db

        class Adapter:
            def __init__(self):
                self.sent = []
                self.emit_warning = AsyncMock()

            def extract_media(self, text):
                return [], text

            def extract_images(self, text):
                return [], text

            async def _prepare_strict_transport(self, method, **kwargs):
                def change(conn):
                    record = _load(conn, _key(task_id))
                    record['payload']['text'] = 'updated while preparing'
                    _save(conn, _key(task_id), record)
                self_db._execute_write(change)

            @single_attempt_transport
            async def send(self, **kwargs):
                self.sent.append(kwargs)
                return SendResult(success=True)

        self_db = db
        adapter = Adapter()
        runner._delivery_adapter_for = lambda source: adapter
        runner._thread_metadata_for_source = lambda source, message_id: None
        agent = MagicMock()
        agent.run_conversation.return_value = {'final_response':'completed', 'messages':[]}
        with patch('gateway.run._resolve_runtime_agent_kwargs', return_value={'api_key':'inert'}), \
             patch('gateway.run._load_gateway_config', return_value={}), \
             patch('run_agent.AIAgent', return_value=agent), \
             patch('gateway.bot_peer_notices.automatic_notices_allowed', return_value=True):
            await runner._run_background_task('work', source, task_id)

        stored = db._execute_write(lambda conn: _load(conn, _key(task_id)))
        assert stored['status'] == 'completed' and stored['delivery'] == 'pending'
        assert agent.run_conversation.call_count == 1
        assert adapter.sent == []
        adapter.emit_warning.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_live_reply_preserves_original_when_saved_copy_is_redacted(tmp_path, monkeypatch):
    task_id = 'live-redaction'
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat')
    response = 'before synthetic-private-value after'
    monkeypatch.setattr('agent.redact.redact_sensitive_text',
                        lambda text: text.replace('synthetic-private-value', '[REDACTED]'))
    with SessionDB(tmp_path / 'state.db') as db:
        runner = _make_runner()
        runner._session_db = db

        class Adapter:
            def __init__(self):
                self.sent = []

            def extract_media(self, text):
                return [], text

            def extract_images(self, text):
                return [], text

            @single_attempt_transport
            async def send(self, **kwargs):
                self.sent.append(kwargs['content'])
                return SendResult(success=True)

        adapter = Adapter()
        runner._delivery_adapter_for = lambda source: adapter
        runner._thread_metadata_for_source = lambda source, message_id: None
        agent = MagicMock()
        agent.run_conversation.return_value = {'final_response':response, 'messages':[]}
        with patch('gateway.run._resolve_runtime_agent_kwargs', return_value={'api_key':'inert'}), \
             patch('gateway.run._load_gateway_config', return_value={}), \
             patch('run_agent.AIAgent', return_value=agent):
            await runner._run_background_task('work', source, task_id)

        stored = db._execute_write(lambda conn: _load(conn, _key(task_id)))
        assert stored['payload']['text'] == 'before [REDACTED] after'
        assert stored['delivery'] == 'delivered'
        assert len(adapter.sent) == 1 and response in adapter.sent[0]