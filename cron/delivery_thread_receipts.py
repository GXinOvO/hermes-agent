import json
import logging

logger = logging.getLogger(__name__)


def _audit(value):
    from cron.strict_thread_delivery import digest
    version = value['binding']['version']
    items = []
    for target, item in zip(value['binding']['targets'], value['items']):
        kind = target['kind'] if version == 2 else 'thread'
        entry = {'stage': item['stage'], 'receipt_sha256': item['receipt_sha256']}
        if version == 2:
            entry.update(kind=kind, route_sha256=digest(target['route']))
        if kind == 'thread':
            plan = target['plan'] if version == 2 else target
            entry.update(workspace=plan['workspace'], parent=plan['parent'],
                         root=item['receipt']['ts'] if item['receipt'] else None)
        elif kind == 'native-thread':
            plan = target['plan']
            entry.update(platform=plan['platform'], workspace=plan['scope_id'], parent=plan['parent'],
                         root=item['receipt']['root'] if item['receipt'] else None)
        items.append(entry)
    return {'version': version, 'binding_sha256': value['binding_sha256'], 'items': items}


def retain_receipts(conn):
    from cron.strict_thread_delivery import validate_row

    rows = conn.execute("SELECT * FROM deliveries "
        "WHERE status IN ('delivered','failed','unknown','suppressed') AND thread_json IS NOT NULL").fetchall()
    for row in rows:
        try:
            value = validate_row(row)
            if value['binding']['job'].get('execution_id') != row['execution_id']:
                raise ValueError('strict thread terminal execution identity unverified')
            audit = _audit(value)
            encoded = json.dumps(audit, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError, KeyError, OverflowError):
            logger.warning('Cron delivery %s retained unchanged: terminal thread receipt invalid',
                           row['execution_id'], exc_info=True)
            continue
        conn.execute('UPDATE deliveries SET thread_receipts_json=?, thread_json=NULL WHERE execution_id=?',
                     (encoded, row['execution_id']))
