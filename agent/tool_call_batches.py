"""Normalize local bridge batches before persistence and per-tool execution."""

from copy import deepcopy
import json
from typing import Any, Callable

from agent.message_sanitization import coalesce_tool_call_id, deterministic_call_id, uniquify_tool_call_ids
from agent.transports.types import ToolCall
from tools.connectors import is_connector_name
from tools.connectors.gateway.config import MAX_CALLS_PER_DISPATCH
from tools.tool_search_catalog import TOOL_CALL_NAME
from tools.tool_search_validation import normalize_tool_call_entries

_UNSET = object()


def expand_local_tool_batches(tool_calls: list, *, provider_data: dict | None = None) -> list:
    """Give every local entry the ordinary agent execution and persistence path.

    Registry-only batch dispatch loses the live agent's inline tools, guardrails,
    and per-server concurrency policy. Split the *new* assistant turn instead,
    before it is persisted; never rewrite an earlier conversation message.
    Connector-only batches keep their existing dispatcher and wire shape.
    """
    expanded, replacements = [], {}
    for call in tool_calls:
        if call.function.name != TOOL_CALL_NAME:
            expanded.append(call)
            continue
        try:
            args = call.function.arguments
            entries, error = normalize_tool_call_entries(json.loads(args) if isinstance(args, str) else args)
        except (TypeError, ValueError, AttributeError):
            entries, error = [], True
        if (error or not 1 < len(entries) <= MAX_CALLS_PER_DISPATCH
                or all(is_connector_name(entry["name"]) for entry in entries)):
            # Leave malformed/oversized requests intact for the dispatcher's error.
            expanded.append(call)
            continue
        parent_id = coalesce_tool_call_id(call)
        children = []
        for index, entry in enumerate(entries):
            arguments = json.dumps({"calls": [entry]}, ensure_ascii=False)
            if index == 0:
                child = deepcopy(call)
                child.function.arguments = arguments
            else:
                # A new entry must not inherit the parent's Responses item id or
                # provider signature. IDs are deterministic for stable replay.
                child = ToolCall(
                    id=deterministic_call_id(TOOL_CALL_NAME, parent_id + arguments, index),
                    name=TOOL_CALL_NAME, arguments=arguments,
                )
            expanded.append(child)
            children.append(child)
        replacements[parent_id] = children
    uniquify_tool_call_ids(expanded)
    if provider_data and replacements:
        replace_native_tool_blocks(provider_data, replacements)
    return expanded


def replace_native_tool_blocks(provider_data: dict, replacements: dict) -> None:
    """Native providers replay ordered blocks instead of rebuilding them from
    tool_calls. Replace the matching tool block in place, preserving signed
    thinking/text blocks and their order exactly.

    ``replacements`` maps a call id to that call's ordered replacement payloads. A provider id
    shared by several calls pairs block-by-block in call order, and the last block for an id
    absorbs any remainder so one call can still expand into several bridge entries.
    """
    for key, block_id, rebuild in _SIDECARS:
        if blocks := provider_data.get(key):
            provider_data[key] = _rewrite_blocks(blocks, replacements, block_id, rebuild)


def _rewrite_blocks(blocks: list, replacements: dict, block_id: Callable[[dict], Any],
                    rebuild: Callable[[dict, Any], dict]) -> list:
    """Rewrite the tool blocks of one native sidecar, keeping every other block in place and order."""
    pending = {cid: list(payloads) for cid, payloads in replacements.items() if cid}
    occurrences: dict[str, int] = {}
    for block in blocks:
        cid = block_id(block)
        if cid in pending:
            occurrences[cid] = occurrences.get(cid, 0) + 1
    seen: dict[str, int] = {}
    out: list = []
    for block in blocks:
        cid = block_id(block)
        payloads = pending.get(cid) if cid else None
        if not payloads:
            out.append(block)
            continue
        seen[cid] = index = seen.get(cid, 0) + 1
        out.append(rebuild(block, payloads.pop(0)))
        if index == occurrences[cid]:
            # Last block carrying this id: take the tail (one call expanded into several entries).
            out.extend(rebuild(block, payload) for payload in payloads)
            pending[cid] = []
    return out


def _anthropic_block_id(block: dict) -> Any:
    return block.get("id") if block.get("type") == "tool_use" else None


def _anthropic_block(block: dict, child: Any) -> dict:
    return {**block, "id": coalesce_tool_call_id(child), "name": child.function.name,
            **_input_field(child, block)}


def _bedrock_block_id(block: dict) -> Any:
    tool = block.get("toolUse")
    return tool.get("toolUseId") if isinstance(tool, dict) else None


def _bedrock_block(block: dict, child: Any) -> dict:
    tool = block["toolUse"]
    return {**block, "toolUse": {**tool, "toolUseId": coalesce_tool_call_id(child),
                                 "name": child.function.name, **_input_field(child, tool)}}


_SIDECARS = (
    ("anthropic_content_blocks", _anthropic_block_id, _anthropic_block),
    ("bedrock_content_blocks", _bedrock_block_id, _bedrock_block),
)


def _input_field(child: Any, block: dict) -> dict:
    """``{"input": ...}`` for the rewritten block, or nothing when the arguments cannot be trusted."""
    value = _replay_input(getattr(child.function, "arguments", None))
    return {} if value is _UNSET else {"input": value}


def _replay_input(arguments: Any) -> Any:
    """Payload for a rewritten block's ``input`` — never a guess.

    Callers run this on calls whose name was just repaired, so ``arguments`` may still be the raw
    provider shape: a dict (json-mode providers), blank (normalized to ``{}`` by
    ``validate_tool_calls``), or JSON cut off mid-stream. Only a dict payload, or the documented
    blank → ``{}`` normalization, replaces the block's own input; unparseable text keeps the
    provider's payload and leaves the turn to argument validation's retry/refusal path.
    """
    if isinstance(arguments, dict):
        return deepcopy(arguments)
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return _UNSET
        return parsed if isinstance(parsed, dict) else _UNSET
    if arguments is None:
        return {}
    return _UNSET
