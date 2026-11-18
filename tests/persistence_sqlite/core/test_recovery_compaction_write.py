from agent.context_compressor import ContextCompressor, _DB_PERSISTED_MARKER
from agent.conversation_loop import _EMPTY_TOOL_RESPONSE_NUDGE
from hermes_state import SessionDB


def test_micro_commit_does_not_first_write_recovery_scaffolding(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_message('s', 'user', 'old request')
        db.append_message('s', 'assistant', 'old reply')
        call = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'real-call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]}
        tool = {'role': 'tool', 'content': 'real result', 'tool_call_id': 'real-call'}
        sentinel = {'role': 'assistant', 'content': '(empty)', '_empty_recovery_synthetic': True}
        nudge = {'role': 'user', 'content': _EMPTY_TOOL_RESPONSE_NUDGE, '_empty_recovery_synthetic': True}
        messages = [{'role': 'user', 'content': 'current request'}, call, tool, sentinel, nudge,
                    {'role': 'assistant', 'content': 'done'}]
        compressor = ContextCompressor.__new__(ContextCompressor)
        compressor._session_db, compressor._session_id = db, 's'

        assert compressor._sync_micro_compact_to_db(messages) is True

        stored = db.get_messages('s')
        assert [row['role'] for row in stored] == ['user', 'assistant', 'tool', 'assistant']
        assert stored[1]['tool_calls'][0]['id'] == stored[2]['tool_call_id'] == 'real-call'
        assert stored[2]['content'] == 'real result'
        assert not sentinel.get(_DB_PERSISTED_MARKER)
        assert not nudge.get(_DB_PERSISTED_MARKER)
        assert all(message.get(_DB_PERSISTED_MARKER) for message in [messages[0], call, tool, messages[-1]])
        assert db.get_session('s')['message_count'] == 4

    with SessionDB(db_path=tmp_path / 'state.db') as reopened:
        assert [row['content'] for row in reopened.get_messages('s')] == ['current request', '', 'real result', 'done']


def test_rotated_commit_does_not_mark_unsaved_scaffolding(tmp_path):
    from types import SimpleNamespace
    from agent.conversation_compression import _publish_rotated_compaction
    from tests.agent import test_compression_concurrent_fork as fixtures

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='test')
        agent = fixtures._build_agent_with_db(db, 'parent')
        call = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'real-call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]}
        tool = {'role': 'tool', 'content': 'real result', 'tool_call_id': 'real-call'}
        sentinel = {'role': 'assistant', 'content': '(empty)', '_empty_recovery_synthetic': True}
        nudge = {'role': 'user', 'content': _EMPTY_TOOL_RESPONSE_NUDGE, '_empty_recovery_synthetic': True}
        compressed = [{'role': 'user', 'content': 'current request'}, call, tool, sentinel, nudge,
                      {'role': 'assistant', 'content': 'done'}]

        _publish_rotated_compaction(agent, [], compressed, new_system_prompt='sys',
            lease=SimpleNamespace(holder=None, ttl=60, watermark=0), old_session_id='parent',
            compressed_user_turn_outcome='already_present')

        child = agent.session_id
        assert child != 'parent'
        stored = db.get_messages(child)
        assert [row['role'] for row in stored] == ['user', 'assistant', 'tool', 'assistant']
        assert stored[1]['tool_calls'][0]['id'] == stored[2]['tool_call_id'] == 'real-call'
        assert not sentinel.get(_DB_PERSISTED_MARKER)
        assert not nudge.get(_DB_PERSISTED_MARKER)
        assert all(message.get(_DB_PERSISTED_MARKER) for message in [compressed[0], call, tool, compressed[-1]])
        assert db.get_session(child)['message_count'] == 4
        assert db.get_session(child)['tool_call_count'] == 1

    with SessionDB(db_path=tmp_path / 'state.db') as reopened:
        assert [row['content'] for row in reopened.get_messages(child)] == ['current request', '', 'real result', 'done']
