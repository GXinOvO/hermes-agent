"""Content-free destinations attempted during one auxiliary call."""
from typing import Any, Optional


def record_route_info(
    route_info: Optional[dict[str, str]], provider: Optional[str], model: Optional[str], *,
    base_url: str = "", main_runtime: Optional[dict[str, Any]] = None,
) -> None:
    """Publish the current destination, retaining whether this call already tried the main backend."""
    if route_info is None:
        return
    from agent.backend_identity import BackendIdentity, should_skip_candidate
    from hermes_cli.providers import normalize_provider

    route_info.update(provider=provider or "auto", model=model or "default", base_url=base_url)
    main = main_runtime or {}
    actual = BackendIdentity.build(provider=normalize_provider(provider or ""), model=model, base_url=base_url)
    main_identity = BackendIdentity.build(
        provider=normalize_provider(main.get("provider") or ""), model=main.get("model"),
        base_url=main.get("base_url"),
    )
    if should_skip_candidate(actual, main_identity):
        route_info["main_route_attempted"] = "true"
