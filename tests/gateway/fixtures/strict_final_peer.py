"""Private subprocess peer: actual SQLite admission/settlement + fake transport only."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, _ExtractedResponse
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport

home, mode = Path(sys.argv[1]), sys.argv[2]
dl._db_path = lambda: home / 'ledger.db'
wire = home / 'wire.jsonl'


class Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.edits = 0

    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        if mode == 'claimed-crash':
            os._exit(73)
        # Actual platform-style split: first accepted chunk, then crash or ambiguous failure.
        for index in range(0, len(content), 300):
            self.write_wire(content[index:index + 300])
            if mode == 'strict-crash':
                os._exit(73)
            if mode == 'partial-failure':
                return SendResult(success=False, error='HTTP 503 connection reset', retryable=True,
                                  raw_response={'partial_overflow':True})
        return SendResult(success=True, message_id='ack')

    def write_wire(self, content):
        with wire.open('a') as stream:
            stream.write(json.dumps({'pid':os.getpid(), 'content':content}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    @single_attempt_transport
    async def send_document(self, chat_id, file_path, **kwargs):
        self.write_wire('document:' + Path(file_path).read_text())
        if mode == 'attachment-crash':
            os._exit(73)
        return SendResult(success=False, error='HTTP 503 attachment ACK lost', retryable=True)

    async def edit_message(self, **kwargs):
        self.edits += 1
        raise AssertionError('strict stream must not edit')

    async def send_draft(self, *args, **kwargs):
        raise AssertionError('strict stream must not draft')

    async def send_stream_frame(self, *args, **kwargs):
        raise AssertionError('strict stream must not open native frame')


def states():
    with dl._connect() as conn:
        return list(conn.execute('SELECT state,attempts,recovery_policy,provenance_json,payload_kind '
                                 'FROM delivery_obligations'))


async def main():
    adapter = Adapter()
    if mode == 'recover':
        rows = dl.sweep_recoverable()
        for row in rows:
            await adapter.send(row['chat_id'], row['content'])
            dl.mark_delivered(row['obligation_id'])
        print(json.dumps({'recovered':len(rows), 'states':states()}))
        return
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input, settle_session_input
    from gateway.session_delivery_policy import automation_delivery_scope, arm_tool_witness
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    db = SessionDB(home / 'admissions.db')
    db.create_session('sid', source='slack')
    epoch = begin_runtime_epoch(db, instance_id='strict-fixture')
    route = 'agent:main:slack:dm:chat'
    identity = '["completion","process-123",1.25]'
    payload = {'text':'completed process result', 'native_text_v1':{
        'route':route, 'automation':{'owner':'sid', 'identity':identity, 'result_recovery_v1':'consume_only'}}}
    admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                        request_id=identity, payload=payload)
    row = claim_session_input(db, epoch=epoch, session_id='sid')
    source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
    event = MessageEvent(text=payload['text'], source=source, message_id=identity, internal=True)
    consumer = None
    async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
        arm_tool_witness(SimpleNamespace(tool_start_callback=None))
        if mode.startswith('stream-'):
            consumer = GatewayStreamConsumer(adapter, 'chat', StreamConsumerConfig(transport=mode[7:], cursor=''))
            consumer.on_delta('first streamed preview')
            consumer.on_delta(None)
            consumer.on_commentary('interim commentary must not create an early final')
            consumer.on_delta('second segment')
            consumer.finish('sealed final')
            await consumer.run()
            assert not consumer.final_content_delivered and not consumer.already_sent
            assert not wire.exists()
    if mode != 'unsettled-result':
        settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
            outcome='completed', result={'result':{'final_response':'sealed final','messages':[]}, 'usage':{}})
    if mode == 'claim-storage-failure':
        dl.claim_automation_send = lambda **kwargs: (_ for _ in ()).throw(OSError('ledger unavailable'))
    if mode == 'ack-storage-failure':
        dl.mark_delivered = lambda *args: (_ for _ in ()).throw(OSError('receipt unavailable'))
    if mode.startswith('attachment-'):
        path = home / 'report.txt'
        path.write_text('exact local report')
        extracted = _ExtractedResponse(text_content='', images=[], media_files=[], local_files=[str(path)],
                                      force_document_attachments=False, pre_extract='report')
        results = []
        await adapter._deliver_attachments(event, extracted, {}, anything_sent=False, record_delivery=results.append)
        await adapter._deliver_attachments(event, extracted, {}, anything_sent=False, record_delivery=results.append)
    else:
        text = 'long answer ' * 100 if mode in {'strict-crash','partial-failure'} else 'sealed final'
        results = [(await adapter.send_final_ledgered(event, route, text, {}, reply_to=None))[0]]
        results += [(await adapter.send_final_ledgered(event, route, text, {}, reply_to=None))[0]]
    print(json.dumps({'states':states(), 'success':[r.success for r in results], 'edits':adapter.edits}))
    db.close()


asyncio.run(main())
