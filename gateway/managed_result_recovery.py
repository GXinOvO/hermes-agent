"""Consume-only owner-startup recovery for sealed managed turn completions."""
import json
import logging

from hermes_state_managed_results import PREFIX, consume_result, expire_results
from hermes_state_runtime import RuntimeStoreError


def recover_completed_managed_results(db, *, profile_id, epoch):
    expire_results(db, profile_id=profile_id, epoch=epoch)
    with db._read_ctx() as conn:
        keys = [row[0] for row in conn.execute('SELECT key FROM state_meta WHERE key LIKE ?', (PREFIX + '%',))]
    recovered = 0
    for key in keys:
        try:
            consume_result(db, profile_id=profile_id, epoch=epoch, admission_id=key[len(PREFIX):])
        except (RuntimeStoreError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logging.getLogger(__name__).warning('Managed result recovery refused: %s', type(exc).__name__)
        else:
            recovered += 1
    return recovered
