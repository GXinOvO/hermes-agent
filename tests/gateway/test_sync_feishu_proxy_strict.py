import logging
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_transport_scope
from plugins.platforms.feishu.adapter import FeishuAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_feishu_send_proxy_failure_hides_credentials_in_result_and_logs(monkeypatch, caplog, strict):
    secret = "proxy-secret-qzx"
    proxy = f"http://alice:{secret}@127.0.0.1:1"
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={"app_id": "cli_x", "app_secret": "inert"}))
    adapter._client = object()
    adapter._send_raw_message = AsyncMock(side_effect=OSError(f"cannot connect using {proxy}"))
    monkeypatch.setattr("plugins.platforms.feishu.outbound.asyncio.sleep", AsyncMock())
    with caplog.at_level(logging.WARNING):
        if strict:
            with strict_transport_scope():
                result = await adapter.send("oc_x", "hello")
        else:
            result = await adapter.send("oc_x", "hello")
    assert not result.success
    assert adapter._send_raw_message.await_count == (1 if strict else 3)
    assert secret not in str(result.error)
    assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "animation"])
async def test_feishu_strict_download_failure_hides_proxy_secret_without_fallback(monkeypatch, caplog, kind):
    secret = "proxy-secret-qzx"
    proxy = f"http://alice:{secret}@127.0.0.1:1"
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={"app_id": "cli_x", "app_secret": "inert"}))
    adapter._download_remote_image = AsyncMock(side_effect=OSError(f"cannot connect using {proxy}"))
    adapter._download_remote_document = AsyncMock(side_effect=OSError(f"cannot connect using {proxy}"))
    adapter.send = AsyncMock()
    with caplog.at_level(logging.WARNING), strict_transport_scope():
        result = await getattr(adapter, f"send_{kind}")("oc_x", f"https://example.com/a.{kind}")
    assert not result.success
    adapter.send.assert_not_awaited()
    assert secret not in str(result.error)
    assert secret not in caplog.text
