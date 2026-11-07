import queue
from unittest.mock import patch

import pytest

from tools import async_delegation as ad
from tools.process_registry_notifications import format_process_notification


@pytest.mark.parametrize('fail_write', [False, True])
def test_delegation_completion_reports_durability_without_changing_work_status(fail_write):
    events = queue.Queue()
    record = {'delegation_id':'durability-test', 'goal':'read only', 'dispatched_at':1,
              'completed_at':2, 'parent_session_id':'parent'}
    with patch('tools.process_registry.process_registry.completion_queue', events), \
         patch.object(ad, '_persist_completion', side_effect=OSError('store unavailable') if fail_write else None):
        ad._push_completion_event(record, {'status':'completed', 'summary':'actual result'}, 'completed')
    event = events.get_nowait()
    assert event['status'] == 'completed'
    assert event['summary'] == 'actual result'
    assert event['result_durability'] == ('failed' if fail_write else 'persisted')
    text = format_process_notification(event)
    assert ('not durably saved' in text) is fail_write
