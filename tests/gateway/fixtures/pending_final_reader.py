"""Separate real SQLite reader / contender, never a model or transport substitute."""
import json
from pathlib import Path
import sys

from gateway.pending_final import claim, pending
from hermes_state import SessionDB

home = Path(sys.argv[1])
if len(sys.argv) > 2 and sys.argv[2] == 'seal':
    import asyncio
    from tests.gateway.test_pending_final import _sealed
    from gateway.session_results import finish_result
    home.mkdir(parents=True, exist_ok=True)
    db, epoch, row, _event, descriptor, _authority = asyncio.run(_sealed(home))
    finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
        result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
    db.close()
    print(json.dumps({'sealed':True}))
    raise SystemExit(0)
db = SessionDB(home / 'state.db')
value = pending(db)[-1]
receipt = claim(db, value['binding']['admission_id'], 'text', 'sealed final', recovering=True)
print(json.dumps({'claimed':receipt[1], 'state':receipt[2]}))
db.close()
