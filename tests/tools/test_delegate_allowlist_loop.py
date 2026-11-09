import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from run_agent import AIAgent
from tools import delegate_tool as delegation


def _response(content=None, calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls),
                                 finish_reason="tool_calls" if calls else "stop")],
        model="unit-allowlist", usage=None,
    )


def _call(name, arguments):
    return SimpleNamespace(id="call-" + name, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def test_native_child_loop_sends_restricted_schema_and_blocks_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n  streaming: false\n", encoding="utf-8")
    monkeypatch.setattr(delegation, "_load_config", dict)
    parent = AIAgent(
        api_key="unit-no-network", base_url="http://127.0.0.1:9/v1", provider="custom",
        api_mode="chat_completions", model="unit-allowlist", platform="cli", quiet_mode=True,
        skip_context_files=True, skip_memory=True, skip_background_review=True,
        enabled_toolsets=["file", "terminal"], max_iterations=5,
    )
    child = delegation._build_child_agent(
        task_index=0, goal="Read fixture", context=None, toolsets=None, model=None,
        max_iterations=5, task_count=1, parent_agent=parent, allowed_tools=["read_file"],
    )
    readable = tmp_path / "readable.txt"
    readable.write_text("UNIT-native-read-value", encoding="utf-8")
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged", encoding="utf-8")
    requests = []
    responses = iter([
        _response(calls=[_call("write_file", {"path": str(protected), "content": "bad"}),
                         _call("read_file", {"path": str(readable)})]),
        _response(content="UNIT-complete"),
    ])
    client = MagicMock()

    def send(**kwargs):
        requests.append(json.loads(json.dumps(kwargs)))
        return next(responses)

    client.chat.completions.create.side_effect = send
    child.client = client
    child._cached_system_prompt = "UNIT transport qualification."
    child._use_prompt_caching = False
    child.compression_enabled = False
    child._disable_streaming = True
    try:
        result = child.run_conversation("Read the authorized fixture")
        assert result["completed"] is True, result
        assert result["final_response"] == "UNIT-complete"
        assert requests
        assert all([t["function"]["name"] for t in r.get("tools", [])] == ["read_file"] for r in requests)
        outputs = [m for m in result["messages"] if m.get("role") == "tool"]
        assert any("UNIT-native-read-value" in str(m.get("content")) for m in outputs)
        assert any("write_file" in str(m.get("content")) for m in outputs)
        assert protected.read_text(encoding="utf-8") == "unchanged"
    finally:
        child.close()
        parent.close()
