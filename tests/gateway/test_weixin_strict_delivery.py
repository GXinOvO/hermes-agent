"""Weixin strict delivery contracts at the real aiohttp/loopback seam."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, strict_transport_scope, supports_strict_transport
from plugins.platforms.weixin import adapter as weixin

SEND = "/ilink/bot/sendmessage"
UPLOAD_URL = "/ilink/bot/getuploadurl"
OPERATIONS = ["send", "send_document", "send_image_file", "send_image", "remote_image", "send_video", "send_voice"]


@asynccontextmanager
async def peer(faults=None):
    state = SimpleNamespace(events=[], faults=faults or {}, counts={}, base="")

    async def handle(request):
        raw = await request.read()
        body = None
        if request.path in (SEND, UPLOAD_URL):
            import json
            body = json.loads(raw)
        state.events.append((request.method, request.path, body))
        index = state.counts.get(request.path, 0)
        state.counts[request.path] = index + 1
        series = state.faults.get(request.path, [])
        fault = series[index] if index < len(series) else None
        if fault == "lost":
            request.transport.abort()
            return web.Response()
        if fault == "503":
            return web.Response(status=503, text="ACK unavailable")
        if fault == "stale":
            return web.json_response({"ret": -14, "errmsg": "session expired"})
        if fault == "rate":
            return web.json_response({"ret": -2, "errmsg": "frequency limit"})
        if fault == "upload_rejected":
            return web.json_response({"ret": -14, "upload_full_url": state.base + "/upload"})
        if fault == "empty_ack":
            return web.json_response({})
        if fault == "nonobject_ack":
            return web.json_response([])
        if fault == "invalid_ack":
            return web.json_response({"ret": False})
        if fault == "redirect":
            return web.json_response({"ret": 0}, status=307, headers={"Location": state.base + request.path})
        if request.path == UPLOAD_URL:
            return web.json_response({"ret": 0, "upload_full_url": state.base + "/upload"})
        if request.path == "/upload":
            return web.Response(headers={"x-encrypted-param": "inert-ciphertext-id"})
        if request.path == "/image.png":
            return web.Response(body=b"inert-image", content_type="image/png")
        return web.json_response({"ret": 0})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        await runner.cleanup()


@asynccontextmanager
async def connected(state, tmp_path, monkeypatch):
    adapter = weixin.WeixinAdapter(PlatformConfig(enabled=True, token="inert-token", extra={
        "account_id": "inert-account", "base_url": state.base, "cdn_base_url": state.base,
        "send_chunk_delay_seconds": 0, "send_chunk_retries": 2,
        "send_chunk_retry_delay_seconds": 0, "rate_limit_circuit_threshold": 99,
    }))
    adapter.MAX_MESSAGE_LENGTH = 12
    adapter._token_store._cache[adapter._token_store._key(adapter._account_id, "peer")] = "inert-context"
    # The download fixture permits only this inert local peer; HTTP itself is unmocked.
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: url == state.base + "/image.png")
    async with weixin._new_session() as session:
        adapter._send_session = session
        yield adapter


def arguments(operation, tmp_path, state):
    if operation == "send":
        return "send", {"chat_id": "peer", "content": "first\n\nsecond\n\nthird"}
    suffix = {"send_document": ".pdf", "send_image_file": ".png", "send_image": ".png",
              "remote_image": ".png", "send_video": ".mp4", "send_voice": ".wav"}[operation]
    path = tmp_path / (operation + suffix)
    path.write_bytes(b"inert-media")
    key = {"send_document": "file_path", "send_image_file": "image_path", "send_image": "image_url",
           "remote_image": "image_url", "send_video": "video_path", "send_voice": "audio_path"}[operation]
    value = state.base + "/image.png" if operation == "remote_image" else str(path)
    method = "send_image" if operation == "remote_image" else operation
    return method, {"chat_id": "peer", key: value, "caption": "caption"}


def posts(state, path=SEND):
    return [body for method, target, body in state.events if method == "POST" and target == path]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ["lost", "503", "stale", "rate", "empty_ack", "nonobject_ack", "invalid_ack", "redirect"])
async def test_strict_public_method_stops_at_first_failed_ack(operation, fault, tmp_path, monkeypatch):
    async with peer({SEND: [fault]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments(operation, tmp_path, state)
        with strict_transport_scope():
            result = await getattr(adapter, method)(**kwargs)
        assert result.success is False, (operation, fault, state.events)
        assert result.error
        assert len(posts(state)) == 1, state.events
        assert posts(state)[0]["msg"]["context_token"] == "inert-context"
        assert adapter._token_store.get(adapter._account_id, "peer") == "inert-context"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_strict_call_success_is_method_verified(operation, tmp_path, monkeypatch):
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments(operation, tmp_path, state)
        assert supports_strict_transport(adapter, method)
        result = await strict_call(adapter, method, **kwargs)
        assert result.success and result.message_id
        assert len(posts(state)) == (3 if operation == "send" else 2)
        assert all(body["msg"]["to_user_id"] == "peer" for body in posts(state))
        assert not supports_strict_transport(adapter, "send_typing")
        assert not supports_strict_transport(adapter, "send_multiple_images")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lost", "503", "stale"])
async def test_strict_send_media_failure_stops_remaining_media_and_text(fault, tmp_path, monkeypatch):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    async with peer({SEND: [fault]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send("peer", f"MEDIA:{first}\nMEDIA:{second}\nremaining text")
        assert not result.success, state.events
        assert len(posts(state)) == 1, state.events
        assert len(posts(state, UPLOAD_URL)) == 1
        assert len(posts(state, "/upload")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [UPLOAD_URL, "/upload", "media"])
@pytest.mark.parametrize("fault", ["lost", "503", "redirect"])
async def test_strict_media_failure_stops_at_actual_http_stage(stage, fault, tmp_path, monkeypatch):
    target = SEND if stage == "media" else stage
    series = [None, fault] if stage == "media" else [fault]
    async with peer({target: series}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments("send_document", tmp_path, state)
        with strict_transport_scope():
            result = await getattr(adapter, method)(**kwargs)
        assert not result.success, state.events
        expected = {UPLOAD_URL: (1, 0, 0), "/upload": (1, 1, 0), "media": (1, 1, 2)}[stage]
        assert tuple(len(posts(state, p)) for p in (UPLOAD_URL, "/upload", SEND)) == expected, state.events


@pytest.mark.asyncio
async def test_strict_send_preflights_missing_second_media_before_first_post(tmp_path, monkeypatch):
    first, missing = tmp_path / "first.pdf", tmp_path / "missing.pdf"
    first.write_bytes(b"one")
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send("peer", f"MEDIA:{first}\nMEDIA:{missing}\ntext")
        assert not result.success
        assert state.events == []


@pytest.mark.asyncio
async def test_strict_send_preflights_overridden_unverified_media_method(tmp_path, monkeypatch):
    first, second = tmp_path / "first.pdf", tmp_path / "second.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        async def unverified(**kwargs):
            raise AssertionError("unverified media reached")
        monkeypatch.setattr(adapter, "send_image_file", unverified)
        with strict_transport_scope():
            result = await adapter.send("peer", f"MEDIA:{first}\nMEDIA:{second}\ntext")
        assert not result.success
        assert "strict_transport_unverified" in (result.error or "")
        assert state.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,expected", [("lost", 3), ("503", 3), ("rate", 3), ("stale", 4)])
async def test_human_text_keeps_retry_and_tokenless_policy(fault, expected, tmp_path, monkeypatch):
    async with peer({SEND: [fault] * 4}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        result = await adapter.send("peer", "one")
        assert not result.success
        assert len(posts(state)) == expected
        tokens = [body["msg"].get("context_token") for body in posts(state)]
        assert tokens == (["inert-context", None, None, None] if fault == "stale" else ["inert-context"] * expected)


@pytest.mark.asyncio
async def test_human_media_keeps_tokenless_retry_then_success(tmp_path, monkeypatch):
    async with peer({SEND: ["stale"]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments("send_document", tmp_path, state)
        result = await getattr(adapter, method)(**kwargs)
        assert result.success
        assert [b["msg"].get("context_token") for b in posts(state)] == ["inert-context", None, None]
        assert posts(state)[0]["msg"]["client_id"] == posts(state)[1]["msg"]["client_id"]


@pytest.mark.asyncio
async def test_human_send_keeps_best_effort_media_and_following_text(tmp_path, monkeypatch):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    async with peer({SEND: ["lost"]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        result = await adapter.send("peer", f"MEDIA:{first}\nMEDIA:{second}\ntext")
        assert result.success
        assert len(posts(state)) == 3
        assert len(posts(state, UPLOAD_URL)) == 2
        assert posts(state)[-1]["msg"]["item_list"][0]["text_item"]["text"] == "text"

@pytest.fixture(autouse=True)
def transport_evidence(monkeypatch, record_property):
    original = peer
    states = []

    @asynccontextmanager
    async def observed(*args, **kwargs):
        async with original(*args, **kwargs) as state:
            states.append(state)
            yield state

    monkeypatch.setitem(globals(), "peer", observed)
    yield
    import json
    record_property("primitive_http_events", json.dumps([s.events for s in states]))


@pytest.mark.asyncio
async def test_aiohttp_default_post_does_not_replay_lost_ack_on_reused_connection():
    import aiohttp
    async with peer({"/primitive": [None, "lost"]}) as state:
        async with weixin._new_session() as session:
            async with session.post(state.base + "/primitive", data=b"warm") as response:
                assert response.status == 200
                await response.read()
            with pytest.raises(aiohttp.ClientError):
                async with session.post(state.base + "/primitive", data=b"lost"):
                    pass
        assert len(posts(state, "/primitive")) == 2


@pytest.mark.asyncio
async def test_strict_text_stops_after_later_chunk_loses_ack(tmp_path, monkeypatch):
    async with peer({SEND: [None, "lost"]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send("peer", "first\n\nsecond\n\nthird")
        assert not result.success
        assert len(posts(state)) == 2
        assert [b["msg"]["item_list"][0]["text_item"]["text"] for b in posts(state)] == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send_image", "send_image_file"])
async def test_strict_image_wrapper_refuses_unverified_document_override(method, tmp_path, monkeypatch):
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        async def unverified(**kwargs):
            raise AssertionError("unverified document reached")
        monkeypatch.setattr(adapter, "send_document", unverified)
        _, kwargs = arguments(method, tmp_path, state)
        with strict_transport_scope(), pytest.raises(RuntimeError, match="strict_transport_unverified"):
            await getattr(adapter, method)(**kwargs)
        assert state.events == []


@pytest.mark.asyncio
async def test_strict_upload_url_rejection_stops_before_cdn_upload(tmp_path, monkeypatch):
    async with peer({UPLOAD_URL: ["upload_rejected"]}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments("send_document", tmp_path, state)
        with strict_transport_scope():
            result = await getattr(adapter, method)(**kwargs)
        assert not result.success
        assert len(posts(state, UPLOAD_URL)) == 1
        assert posts(state, "/upload") == []
        assert posts(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_text", [False, True])
async def test_strict_send_acks_all_media_before_success(with_text, tmp_path, monkeypatch):
    first, second = tmp_path / "first.pdf", tmp_path / "second.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        content = f"MEDIA:{first}\nMEDIA:{second}" + ("\ntext" if with_text else "")
        result = await strict_call(adapter, "send", chat_id="peer", content=content)
        assert result.success and result.message_id
        assert result.message_id == posts(state)[-1]["msg"]["client_id"]
        assert len(posts(state)) == (3 if with_text else 2)
        assert len(posts(state, UPLOAD_URL)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [o for o in OPERATIONS if o != "send"])
@pytest.mark.parametrize("fault", ["lost", "stale", "redirect"])
async def test_strict_each_media_method_stops_without_caption(operation, fault, tmp_path, monkeypatch):
    # send_voice intentionally inserts a human-readable attachment caption when empty.
    faults = [None, fault] if operation == "send_voice" else [fault]
    async with peer({SEND: faults}) as state, connected(state, tmp_path, monkeypatch) as adapter:
        method, kwargs = arguments(operation, tmp_path, state)
        kwargs["caption"] = ""
        result = await strict_call(adapter, method, **kwargs)
        assert not result.success
        assert len(posts(state)) == len(faults)
        assert len(posts(state, UPLOAD_URL)) == 1
        assert len(posts(state, "/upload")) == 1
        assert posts(state)[-1]["msg"]["item_list"][0]["type"] != weixin.ITEM_TEXT
        assert all(body["msg"]["context_token"] == "inert-context" for body in posts(state))


@pytest.mark.asyncio
async def test_strict_text_refuses_embedded_images_before_any_media_or_text(tmp_path, monkeypatch):
    first = tmp_path / "first.pdf"
    first.write_bytes(b"one")
    async with peer() as state, connected(state, tmp_path, monkeypatch) as adapter:
        result = await strict_call(adapter, "send", chat_id="peer",
                                  content=f"MEDIA:{first}\n![image]({state.base}/image.png)\ntext")
        assert not result.success
        assert "send_image" in (result.error or "")
        assert state.events == []

