from types import SimpleNamespace

from gateway.session_llm_oneshot import _session_runtime
from hermes_state_runtime import RuntimeStoreError


def runtime_for(authority, ref, target):
    live_runtime = _session_runtime(authority, ref) if ref.session_id in authority.sessions else lambda: None
    from gateway.session_local_migration import legacy_launch_params
    row = authority.persistence.sessions.get(target)
    params, api_mode = legacy_launch_params(row)
    model = params.get('model')
    def resolve():
        live = live_runtime()
        if live is not None:
            return live
        if not model:
            raise RuntimeStoreError('analysis_runtime_unavailable', detail='Stored session has no saved main model')
        from gateway.run import _load_gateway_config
        from gateway.session_local_migration import _endpoint_owner
        from hermes_cli.runtime_provider import resolve_runtime_provider
        provider, base_url = params.get('provider'), params.get('base_url')
        if base_url:
            provider = _endpoint_owner({'provider': provider or 'custom', 'base_url': base_url}, _load_gateway_config())
        if not provider:
            raise RuntimeStoreError('analysis_runtime_unavailable', detail='Stored main provider cannot be resolved exactly')
        main = resolve_runtime_provider(requested=provider, target_model=model, explicit_base_url=base_url)
        if not isinstance(main, dict) or not main.get('provider'):
            raise RuntimeStoreError('analysis_runtime_unavailable')
        route = {key: main[key] for key in ('provider', 'base_url', 'api_key', 'api_mode') if main.get(key)}
        agent = SimpleNamespace(**{**route, 'model': model, **({'api_mode': api_mode} if api_mode else {})})
        return _session_runtime(SimpleNamespace(agent=lambda _: agent), ref)()
    return resolve
