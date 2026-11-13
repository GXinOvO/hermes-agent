def converse_call(api_kwargs: dict, *, stream: bool, on_stream_denied=None, audit_agent=None):
    """Pop the Hermes routing keys and call ``converse`` / ``converse_stream`` (boto3 directly) with the
    adapter's shared recovery: a cachePoint rejection (Nova: toolConfig.tools, #97281) or a replayed sealed
    reasoning rejection resends once without the offending blocks inside the same attempt; a streaming IAM
    denial hands off to ``on_stream_denied(client, kwargs, exc)``; a stale connection evicts the cached
    client so the outer retry builds a fresh pool. Streaming returns the event stream; else a SimpleNamespace."""
    from agent.bedrock_adapter import (_get_bedrock_runtime_client, invalidate_runtime_client,
        is_stale_connection_error, is_streaming_access_denied_error, normalize_converse_response,
        recover_from_cache_point_rejection, recover_from_redacted_reasoning_rejection)
    region = api_kwargs.pop("__bedrock_region__", "us-east-1")
    api_kwargs.pop("__bedrock_converse__", None)
    client = _get_bedrock_runtime_client(region, api_kwargs.get("modelId"))  # the active model's timeout
    method = client.converse_stream if stream else client.converse
    finish = (lambda raw: raw.get("stream", [])) if stream else normalize_converse_response
    try:
        if audit_agent is not None:
            from agent.provider_request_audit import observe_sdk_request
            observe_sdk_request(audit_agent, api_kwargs)
        raw_response = method(**api_kwargs)
    except Exception as exc:
        retry_kwargs = (recover_from_cache_point_rejection(exc, api_kwargs)
                        or recover_from_redacted_reasoning_rejection(exc, api_kwargs))
        if retry_kwargs is not None:
            if audit_agent is not None:
                from agent.provider_request_audit import observe_sdk_request
                observe_sdk_request(audit_agent, retry_kwargs)
            return finish(method(**retry_kwargs))
        if on_stream_denied is not None and is_streaming_access_denied_error(exc):
            return on_stream_denied(client, api_kwargs, exc)
        if is_stale_connection_error(exc):
            invalidate_runtime_client(region)
        raise
    return finish(raw_response)
