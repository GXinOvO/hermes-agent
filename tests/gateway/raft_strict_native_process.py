"""Inert producer/recovery subprocess; never reads an account credential."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from gateway import pending_final as pf
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from plugins.platforms.raft.adapter import RaftAdapter


async def main():
    mode, directory, endpoint = sys.argv[1:4]
    home = Path(directory)
    home.mkdir(parents=True, exist_ok=True)
    profile = home / 'inert-raft'
    profile.mkdir(exist_ok=True)
    credential = profile / 'credential.json'
    if not credential.exists():
        credential.write_text(json.dumps({'apiKey':'sk_agent_inert-test-only', 'agentId':'agent-inert',
                                          'serverId':'server-inert', 'serverUrl':endpoint}))
    os.environ.update(RAFT_PROFILE='inert', RAFT_PROFILE_DIR=str(profile), HERMES_HOME=str(home))
    adapter = RaftAdapter(PlatformConfig(enabled=True))
    db = SessionDB(home / 'state.db')
    counter = home / 'producer-executions.txt'
    if mode in {'produce', 'produce-exit'}:
        db.create_session('sid', source='raft')
        epoch = begin_runtime_epoch(db, instance_id='inert-producer')
        source = SessionSource(platform=Platform('raft'), chat_id='#loopback', chat_type='dm')
        route = 'agent:main:raft:dm:#loopback'
        payload = {'text':'complete once', 'native_text_v1':{'route':route, 'source':source.to_dict(),
                   'automation':{'owner':'sid', 'identity':'native-result', 'result_recovery_v1':'consume_only'}}}
        admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                            request_id='native-result', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        with counter.open('a') as output:
            output.write('executed\n')
            output.flush()
            os.fsync(output.fileno())
        event = MessageEvent(text='complete once', source=source, internal=True, message_id='native-result')
        authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source:adapter))
        descriptor = await pf.prepare(authority, row, event, route, 'sealed-raft-loopback-result')
        finish_result(db, epoch=epoch, row=row, response='sealed-raft-loopback-result', outcome='completed',
                      result={'result':{'final_response':'sealed-raft-loopback-result', 'messages':[],
                                        '_pending_final_v1':descriptor}, 'usage':{}})
        if mode == 'produce-exit':
            os._exit(17)
        success = False
    else:
        begin_runtime_epoch(db, instance_id='fresh-' + mode)
        pf.reconcile_orphans(db)
        value = pf.pending(db)[0]
        from plugins.platforms.raft import strict_delivery
        operation = getattr(strict_delivery, 'publish_pending', None)
        assert callable(operation), 'Raft pending-final completed-result consumer is missing'
        if mode == 'claim-exit':
            prepared = strict_delivery.prepare(adapter, 'send', chat_id='#loopback',
                content=value['parts']['text']['content'], execution_id=value['binding']['admission_id'],
                claim_id=value['binding']['admission_id'] + ':text')
            assert prepared is not None
            assert pf.claim(db, value['binding']['admission_id'], 'text', value['parts']['text']['content'], recovering=True)[1]
            os._exit(18)
        result = await operation(adapter, db, value['binding']['admission_id'], recovering=True)
        success = result.success
    records = pf.pending(db)
    print(json.dumps({'success':success, 'record':records[0], 'producer_count':len(counter.read_text().splitlines())}), flush=True)
    db.close()


if __name__ == '__main__':
    asyncio.run(main())
