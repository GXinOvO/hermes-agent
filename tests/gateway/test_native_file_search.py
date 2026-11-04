"""Real native gateway HTTP grant reaches the Files search route on its pinned profile."""
import json

import httpx
import pytest

from tests.gateway.test_native_http_auth import daemon, headers, ticket


@pytest.mark.platforms("linux")
def test_native_http_search_uses_profile_grant_and_structured_stream(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "示例.py").write_text("中文😀 SEARCH_FIELD = 1\n")
    payload = {"root": str(root), "query": "SEARCH_FIELD", "mode": "content"}
    with daemon(tmp_path) as (home, descriptor), httpx.Client(
            base_url=descriptor["api_origin"], trust_env=False, timeout=20) as client:
        assert client.post("/api/fs/search", json=payload).status_code == 401
        response = client.post("/api/fs/search?profile=default", json=payload,
                               headers=headers(ticket(home, descriptor)))
        assert response.status_code == 200, response.text
        frames = [json.loads(line) for line in response.text.splitlines() if line]
        hits = [h for frame in frames if frame["type"] == "batch" for h in frame["hits"]]
        assert [(h["rel"], h["line"], h["col"]) for h in hits] == [("src/示例.py", 1, 5)]
        assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]
        assert client.post("/api/fs/search?profile=other", json=payload,
                           headers=headers(ticket(home, descriptor))).status_code == 403
