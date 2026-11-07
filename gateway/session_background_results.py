"""Durable result boundaries for detached side tasks, without another task queue."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)


def binding(authority, ref, event_type, policy, inputs):
    return {'profile_id':str(Path(authority.db.db_path).parent.resolve()), 'parent_session_id':ref.session_id,
            'owner_epoch':authority.epoch,
            'event_type':event_type, 'policy_sha256':hashlib.sha256(json.dumps({
                'cwd':policy.cwd, 'model':policy.model, 'config':policy.config_json,
                'request':policy.request_json, 'terminal':policy.terminal_json,
                'toolsets':policy.toolsets, 'inputs':inputs}, sort_keys=True).encode()).hexdigest()}


async def prepare(authority, ref, task_id, event_type, policy, inputs):
    from functools import partial
    from gateway.session_runtime_workers import tracked_write
    from hermes_state_background import begin
    identity = binding(authority, ref, event_type, policy, inputs)
    record = await tracked_write(authority, partial(begin, authority.db, task_id, identity))
    if not record.get('claimed'):
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError('admission_conflict')
    return identity


async def read_snapshot(authority, ref):
    from hermes_state_background import results
    from gateway.session_authorities import owner_scope
    def read():
        with owner_scope(authority):
            return results(authority.db, ref.session_id)
    return await asyncio.to_thread(read)


def seal(authority, task_id, identity, payload):
    from gateway.session_authorities import owner_scope
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_background import complete
    try:
        with owner_scope(authority):
            complete(authority.db, task_id, identity, payload)
        return payload
    except (RuntimeStoreError, ValueError):
        logger.warning('Side result %s refused by its owner or target fence', task_id, exc_info=True)
        return None
    except sqlite3.Error:
        logger.exception('Side result %s is not durably saved', task_id)
        return {**payload, 'text':'error: result_not_durable; preserve this in-memory result before restart.\n' + payload['text']}
