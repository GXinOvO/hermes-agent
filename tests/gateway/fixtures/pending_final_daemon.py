"""Actual gateway boot with an audited file-wire adapter and exact crash barriers.

Only transport and the input driver are fixtures. Admission, in-process TurnRunner,
settlement and startup recovery are the ordinary gateway code.
"""
import asyncio
import json
import os
from pathlib import Path
import runpy
import signal

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport
from hermes_state_runtime import list_session_admissions

HOME = Path(os.environ['HERMES_HOME'])
MODE = os.environ.get('FINAL_PROBE_MODE', 'recover')
KIND = os.environ.get('FINAL_PROBE_KIND', 'automation')
TARGET = 'PROBE_MEDIA' if os.environ.get('FINAL_PROBE_MEDIA') == '1' else 'PROBE_TEXT'


def wire(kind, content):
    with (HOME / 'wire.jsonl').open('a') as stream:
        stream.write(json.dumps({'pid':os.getpid(), 'kind':kind, 'content':content})+'\n')
        stream.flush()
        os.fsync(stream.fileno())


class Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, typing_indicator=False), Platform.SLACK)
        self.set_owner_profile('default')

    @property
    def authorization_is_upstream(self):
        return True

    async def connect(self, **kwargs):
        self._driver = asyncio.create_task(self.drive())
        return True

    async def disconnect(self):
        if self._driver:
            self._driver.cancel()

    async def get_chat_info(self, chat_id):
        return {'id':chat_id}

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        if MODE == 'claimed' and TARGET in content:
            os.kill(os.getpid(), signal.SIGKILL)
        wire('text', content)
        if MODE == 'during' and TARGET in content:
            os.kill(os.getpid(), signal.SIGKILL)
        return SendResult(success=True, message_id='wire-ack')

    @single_attempt_transport
    async def send_document(self, chat_id, file_path, **kwargs):
        if MODE == 'claimed':
            os.kill(os.getpid(), signal.SIGKILL)
        wire('document', Path(file_path).read_text())
        if MODE == 'during':
            os.kill(os.getpid(), signal.SIGKILL)
        return SendResult(success=True, message_id='file-ack')

    async def drive(self):
        from tests.waiting import async_wait_until
        from gateway.session_automation import admit_automation
        if MODE == 'recover':
            return
        runner = self.gateway_runner
        await async_wait_until(lambda: not runner._startup_restore_in_progress, timeout=40)
        await async_wait_until(lambda: (HOME / 'start-probe').exists(), timeout=40)
        source = SessionSource(platform=Platform.SLACK, chat_id='owned-chat', chat_type='dm', user_id='human')
        await self.handle_message(MessageEvent(text='WARM_ROUTE', source=source, message_id='warm'))
        authority = runner.session_authority
        await async_wait_until(lambda: any(r['request_id']=='warm' and r['status']=='terminal'
            for r in list_session_admissions(authority.db, session_id=runner.session_store.get_or_create_session(source).session_id,
                                             pending_only=False)), timeout=40)
        entry = runner.session_store.get_or_create_session(source)
        await async_wait_until(lambda: 'WARM_ROUTE' in (HOME / 'wire.jsonl').read_text(), timeout=40)
        if KIND == 'human':
            await self.handle_message(MessageEvent(text=TARGET, source=source, message_id='probe'))
        else:
            event = MessageEvent(text=TARGET, source=source, internal=True, message_id='probe',
                metadata={'gateway_session_key':entry.session_key, 'gateway_session_id':entry.session_id})
            await admit_automation(authority, self, event, 'probe')


create = GatewayAdapterLifecycleMixin._create_adapter


def create_adapter(self, platform, config):
    if platform == Platform.SLACK:
        adapter = Adapter()
        adapter.gateway_runner = self
        return adapter
    return create(self, platform, config)


GatewayAdapterLifecycleMixin._create_adapter = create_adapter
if MODE == 'before':
    from gateway import session_results
    finish = session_results.finish_result
    def after_terminal(db, *, row, **kwargs):
        result = finish(db, row=row, **kwargs)
        if row['payload'].get('text') == TARGET:
            os.kill(os.getpid(), signal.SIGKILL)
        return result
    session_results.finish_result = after_terminal
runpy.run_module('gateway.run', run_name='__main__')
