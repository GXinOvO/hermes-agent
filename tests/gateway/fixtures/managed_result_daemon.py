"""Stop an actual owner after durable worker finish, before owner settlement."""
import asyncio
from pathlib import Path
import os
import runpy

from gateway import session_managed_worker


async def barrier(authority, session_id, accepted):
    expected = os.environ.get('MANAGED_RECOVERY_MATCH')
    if expected and expected not in accepted.get('final_response', ''):
        return
    Path(os.environ['MANAGED_RECOVERY_BARRIER']).write_text(session_id)
    await asyncio.Future()


session_managed_worker._finalize_managed_refusal = barrier
runpy.run_module('gateway.run', run_name='__main__')
