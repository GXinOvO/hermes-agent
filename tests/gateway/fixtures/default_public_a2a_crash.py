import json
import os
from pathlib import Path
import runpy

from gateway import pending_final
from gateway.platforms.base import BasePlatformAdapter

record = BasePlatformAdapter._record_delivery_obligation


async def exit_before_claim(self, event, session_key, text_content, *args, **kwargs):
    if 'DEFAULT_PUBLIC_07_SEALED' in text_content:
        home = Path(os.environ['HERMES_HOME'])
        db = self.gateway_runner.session_authority.db
        values = [value for value in pending_final.pending(db) if value['reply_to'] == event.message_id]
        assert len(values) == 1 and values[0]['parts']['text']['attempts'] == 0
        (home / 'preclaim-exit.json').write_text(json.dumps({'pending': values[0], 'pid': os.getpid()}))
        os._exit(23)
    return await record(self, event, session_key, text_content, *args, **kwargs)


BasePlatformAdapter._record_delivery_obligation = exit_before_claim
runpy.run_module('gateway.run', run_name='__main__')
