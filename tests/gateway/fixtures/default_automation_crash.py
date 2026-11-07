import json
import os
from pathlib import Path
import runpy

from gateway import pending_final

claim = pending_final.claim


def exit_before_claim(db, admission_id, kind, content, **kwargs):
    if 'DEFAULT_AUTOMATION_07_SEALED' in content:
        value = next(value for value in pending_final.pending(db) if value['binding']['admission_id'] == admission_id)
        assert value['parts']['text']['state'] == 'pending' and value['parts']['text']['attempts'] == 0
        (Path(os.environ['HERMES_HOME']) / 'automation-preclaim-exit.json').write_text(
            json.dumps({'pending': value, 'pid': os.getpid()}))
        os._exit(23)
    return claim(db, admission_id, kind, content, **kwargs)


pending_final.claim = exit_before_claim
runpy.run_module('gateway.run', run_name='__main__')
