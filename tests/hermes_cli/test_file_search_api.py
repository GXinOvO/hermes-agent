"""HTTP search producer contract, using the same authenticated FS boundary as preview."""
import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.web_routers import files


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(files, "_fs_backend", lambda profile=None: None)
    with TestClient(web_server.app) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        yield client


def test_search_one_request_returns_structured_hits_and_terminal_verdict(client, tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("中文😀 VALUE_FIELD\n")
    response = client.post("/api/fs/search", json={"root": str(root), "query": "VALUE_FIELD", "mode": "content"})
    assert response.status_code == 200
    frames = [json.loads(line) for line in response.text.splitlines() if line]
    hits = [h for f in frames if f["type"] == "batch" for h in f["hits"]]
    assert len(hits) == 1 and hits[0]["rel"] == "src/app.py"
    assert hits[0]["col"] == 5 and hits[0]["line"] == 1
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]
    assert response.headers["content-type"].startswith("application/x-ndjson")


def test_search_enforces_managed_boundary(client, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from hermes_cli import file_search_api

    def denied(*args, **kwargs):
        raise HTTPException(403, "Path is outside the managed read boundary")

    monkeypatch.setattr(file_search_api, "_hosted_fs_read_guard", denied)
    response = client.post("/api/fs/search", json={"root": str(tmp_path), "query": "x", "mode": "content"})
    assert response.status_code == 403


def test_search_profile_and_session_owner_are_resolved_before_file_access(client, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from hermes_cli import file_search_api

    async def wrong_owner(session_id, profile):
        assert session_id == "foreign" and profile == "work"
        raise HTTPException(403, "Session belongs to another profile")

    monkeypatch.setattr(file_search_api, "_fs_owned_session", wrong_owner)
    response = client.post("/api/fs/search?profile=work&session_id=foreign", json={"root": str(tmp_path), "query": "x", "mode": "content"})
    assert response.status_code == 403


def decoded_frames(response):
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_search_rejects_unauthenticated_request_before_filesystem_access(client, tmp_path, monkeypatch):
    from hermes_cli import file_search_api

    async def forbidden(*args, **kwargs):
        raise AssertionError("unauthenticated request reached the filesystem")

    monkeypatch.setattr(file_search_api, "_fs_owned_session", forbidden)
    client.headers.pop(web_server._SESSION_HEADER_NAME)
    response = client.post("/api/fs/search", json={"root": str(tmp_path), "query": "x"})
    assert response.status_code == 401


def test_real_managed_root_and_nested_sensitive_scope(client, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "safe.txt").write_text("TOKEN_FIELD\n")
    (managed / "nested" / "pairing").mkdir(parents=True)
    (managed / "nested" / "pairing" / "key.txt").write_text("TOKEN_FIELD\n")
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", str(managed))
    outside = client.post("/api/fs/search", json={"root": str(tmp_path), "query": "TOKEN_FIELD"})
    assert outside.status_code == 403
    frames = decoded_frames(client.post("/api/fs/search", json={
        "root": str(managed), "query": "TOKEN_FIELD", "scope": "**/*", "showIgnored": True,
    }))
    assert [h["rel"] for f in frames if f["type"] == "batch" for h in f["hits"]] == ["safe.txt"]
    frames = decoded_frames(client.post("/api/fs/search", json={
        "root": str(managed), "query": "TOKEN_FIELD", "scope": "nested/pairing", "showIgnored": True,
    }))
    assert not [f for f in frames if f["type"] == "batch"]


def test_second_ssh_hop_is_explicitly_unsupported(client, tmp_path, monkeypatch):
    from hermes_cli import file_search_api

    monkeypatch.setattr(file_search_api, "_fs_backend", lambda profile=None: object())
    response = client.post("/api/fs/search", json={"root": str(tmp_path), "query": "x"})
    assert response.status_code == 501 and "SSH workspace" in response.json()["detail"]


@pytest.mark.parametrize("change", [
    {"query": "x\ny"}, {"query": "x\ry"}, {"query": "x\0y"}, {"maxResults": True},
    {"timeoutMs": "50"}, {"regex": "false"}, {"showIgnored": 1}, {"maxResults": 2001},
    {"timeoutMs": 30001},
])
def test_wire_validation_rejects_coercions_and_unbounded_requests(client, tmp_path, change):
    response = client.post("/api/fs/search", json={"root": str(tmp_path), "query": "x", **change})
    assert response.status_code == 422


@pytest.fixture
def docker_mount(tmp_path, monkeypatch):
    # Both host path and container backing exist: host-first preview semantics
    # must not make a project search inspect the other filesystem's contents.
    host = tmp_path / "visible-host"
    backing = tmp_path / "container-backing"
    host.mkdir()
    backing.mkdir()
    (host / "host.txt").write_text("VALUE_FIELD wrong-host\n")
    (backing / "box.txt").write_text("VALUE_FIELD correct-box\n")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps([f"{backing}:{host}"]))
    return host, backing


def test_docker_search_prefers_owned_mapping_and_keeps_client_root_in_hits(client, docker_mount):
    host, backing = docker_mount
    frames = decoded_frames(client.post("/api/fs/search", json={"root": str(host), "query": "VALUE_FIELD"}))
    all_hits = [h for f in frames if f["type"] == "batch" for h in f["hits"]]
    assert [(h["rel"], h["path"]) for h in all_hits] == [("box.txt", str(host / "box.txt"))]
    assert str(backing) not in json.dumps(frames)


def test_unmapped_docker_search_cannot_fall_back_to_existing_host_path(client, docker_mount):
    host, backing = docker_mount
    (backing / "box.txt").unlink()
    backing.rmdir()
    response = client.post("/api/fs/search", json={"root": str(host), "query": "VALUE_FIELD"})
    assert response.status_code == 404


def test_real_profile_session_ownership_precedes_search(client, tmp_path, monkeypatch):
    from hermes_cli import profiles
    from hermes_state import SessionDB

    home = tmp_path / "home"
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal:\n  backend: local\n")
    (work / "config.yaml").write_text("terminal:\n  backend: local\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    for location, sid in ((home, "owned-default"), (work, "owned-work")):
        db = SessionDB(location / "state.db")
        try:
            db.create_session(sid, source="cli")
        finally:
            db.close()
    payload = {"root": str(tmp_path), "query": "NOT_A_HIT"}
    owned = client.post("/api/fs/search?profile=work&session_id=owned-work", json=payload)
    assert decoded_frames(owned)[-1]["type"] == "done"
    foreign = client.post("/api/fs/search?profile=work&session_id=owned-default", json=payload)
    assert foreign.status_code == 404


def test_session_docker_scope_uses_only_its_mounted_workspace(client, tmp_path, monkeypatch):
    from hermes_cli import file_search_api

    own = tmp_path / "own-project"
    foreign = tmp_path / "profile-project"
    own.mkdir()
    foreign.mkdir()
    (own / "own.txt").write_text("VALUE_FIELD own\n")
    (foreign / "foreign.txt").write_text("VALUE_FIELD foreign\n")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_CWD", str(foreign))
    monkeypatch.delenv("TERMINAL_DOCKER_VOLUMES", raising=False)
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))

    async def owned(session_id, profile):
        assert session_id == "search-session"
        return {"id": session_id, "cwd": str(own)}

    monkeypatch.setattr(file_search_api, "_fs_owned_session", owned)
    response = client.post("/api/fs/search?session_id=search-session", json={"root": "/workspace", "query": "VALUE_FIELD"})
    frames = decoded_frames(response)
    assert [(h["rel"], h["path"]) for f in frames if f["type"] == "batch" for h in f["hits"]] == [
        ("own.txt", "/workspace/own.txt"),
    ]
    (own / "own.txt").unlink()
    own.rmdir()
    response = client.post("/api/fs/search?session_id=search-session", json={"root": "/workspace", "query": "VALUE_FIELD"})
    assert response.status_code in (404, 409)


def test_http_disconnect_reaps_search_before_its_deadline(tmp_path, monkeypatch):
    import asyncio
    import shutil
    import socket
    import threading

    import httpx
    import uvicorn

    from hermes_cli import file_search_api
    from tests.waiting import wait_until

    (tmp_path / "flood.txt").write_text(("VALUE_FIELD " + "x" * 100 + "\n") * 50000)
    children = []
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr("hermes_cli.file_search.resolve_rg", lambda: shutil.which("rg"))
    monkeypatch.setattr(file_search_api, "_fs_backend", lambda profile=None: None)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(web_server.app, lifespan="off", log_level="error",
                                         access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started or not thread.is_alive(), what="test uvicorn listen")
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False,
                          timeout=10, headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}) as http:
            with http.stream("POST", "/api/fs/search", json={
                "root": str(tmp_path), "query": "VALUE_FIELD", "timeoutMs": 30000, "maxResults": 2000,
            }) as response:
                assert response.status_code == 200
                assert any(json.loads(line)["type"] == "batch" for line in response.iter_lines() if line)
                assert children and any(child.returncode is None for child in children)
            wait_until(lambda: all(child.returncode is not None for child in children),
                       timeout=5, what="HTTP disconnect rg cleanup before 30-second deadline")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
