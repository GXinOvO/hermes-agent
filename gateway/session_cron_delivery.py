from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


def snapshot(authority, chat_id):
    from gateway.config import Platform
    from gateway.session_detached import route_entry
    live = authority.sessions.get(chat_id)
    entry = route_entry(authority, live.route) if live is not None else None
    if (live is None or live.source is None or live.source.platform != Platform.LOCAL
            or entry is None or entry.suspended or authority.db.get_session(chat_id) is None):
        raise RuntimeStoreError('admission_conflict')
    return {'profile': authority.profile_id, 'chat': chat_id, 'route': live.route,
            'source': live.source.to_dict(), 'target': entry.session_id,
            'database': str(Path(authority.db.db_path).resolve())}


async def admit(authority, identity, epoch):
    from gateway.config import Platform
    from gateway.session_authorities import owner_scope
    from gateway.session_automation import admit_route_wake
    from gateway.session_cron import owner_for_home
    from gateway.session_local_recovery import local_adapter_map
    expected = {key: value for key, value in identity.items() if key not in {'text', 'failure'}}

    def guard(_conn):
        owner = owner_for_home(Path(identity['database']).parent)
        if (owner is None or owner[0] is not authority or authority.epoch != epoch
                or snapshot(authority, identity['chat']) != expected):
            raise RuntimeStoreError('admission_conflict')

    with owner_scope(authority):
        live = authority.sessions.get(identity['chat'])
        if live is None:
            raise RuntimeStoreError('admission_conflict')
        async with live.mutation_lock:
            guard(None)
            source = live.source
            adapter = local_adapter_map(authority)[Platform.LOCAL]
            metadata = {'notification_category': 'diagnostic'} if identity['failure'] else {}
            return await admit_route_wake(authority.runner, adapter, source, identity['text'], 'cron', metadata,
                                          _authorize_write=guard)
