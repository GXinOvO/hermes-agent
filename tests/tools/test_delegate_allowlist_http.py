import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from run_agent import AIAgent
from tools import delegate_tool as delegation


def test_public_delegate_uses_exact_tools_on_real_http_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n  streaming: false\n", encoding="utf-8")
    monkeypatch.setattr(delegation, "_load_config", dict)
    readable = tmp_path / "input.txt"
    readable.write_text("native-http-read-marker", encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            assert not request.get("stream")
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "unit-read", "type": "function",
                           "function": {"name": "read_file", "arguments": json.dumps({"path": str(readable)})}}]}
                reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "native-http-finished"}
                reason = "stop"
            payload = {"id": "unit-response", "object": "chat.completion", "created": 0, "model": "unit-http",
                       "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    parent = AIAgent(api_key="UNIT-local-only", base_url=url, provider="custom", api_mode="chat_completions",
                     model="unit-http", platform="cli", quiet_mode=True, skip_context_files=True,
                     skip_memory=True, skip_background_review=True, enabled_toolsets=["file", "terminal"])
    try:
        result = json.loads(delegation.delegate_task(
            tasks=[{"goal": "Read the authorized HTTP fixture", "allowed_tools": ["read_file"]}],
            parent_agent=parent, credentials_cfg={"model": "unit-http", "base_url": url, "api_key": "UNIT-local-only",
                                                  "api_mode": "chat_completions", "fallback_providers": []},
        ))
        entry = result["results"][0]
        assert entry["status"] == "completed", result
        assert entry["summary"] == "native-http-finished"
        assert len(requests) == 2
        assert all([t["function"]["name"] for t in request.get("tools", [])] == ["read_file"] for request in requests)
        tool_messages = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
        assert any("native-http-read-marker" in str(m["content"]) for m in tool_messages)
        assert any(t.get("tool") == "read_file" for t in entry["tool_trace"])
    finally:
        parent.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
