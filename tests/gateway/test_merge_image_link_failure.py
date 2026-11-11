"""A refused batch image keeps its link as text. Upstream's fallback dropped that link from the
retry set even when its own text post was refused, so with the sibling upload succeeding the link
was never delivered and nothing said so (Slack and Mattermost).

The seam under test is the real ``send_multiple_images`` against a loopback image host. A refused
link is re-attempted only when its post was DEFINITELY refused; one whose post may already be in
the chat (transient failure) is never re-sent and is reported as ``delivery_uncertain``.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult

CAP = 64 * 1024  # gateway.max_inbound_media_bytes for this file
HUGE = CAP * 16  # a body the adapters must refuse: over the cap, refused whole
PNG = b"\x89PNG\r\n\x1a\n"


class _ImageHost(BaseHTTPRequestHandler):
    """Loopback chunked image of ``?size=N`` bytes; the client stops reading an over-cap body."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        size = int(self.path.split("size=")[1]) if "size=" in self.path else len(PNG)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        written = 0
        try:
            while written < size:
                piece = b"\0" * min(16 * 1024, size - written)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                written += len(piece)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


@pytest.fixture
def image_host():
    from hermes_constants import get_hermes_home
    from tools import url_safety

    (get_hermes_home() / "config.yaml").write_text(
        f"gateway:\n  max_inbound_media_bytes: {CAP}\nsecurity:\n  allow_private_urls: true\n")
    url_safety._reset_allow_private_cache()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHost)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    url_safety._reset_allow_private_cache()


class _PostLog:
    """Posts that reached the platform, in order: ``(text, ok)``. ``payloads`` keeps the post bodies
    (a Mattermost batch post carries its ``file_ids``); ``uploads`` counts file uploads."""

    def __init__(self):
        self.posts: list[tuple[str, bool]] = []
        self.payloads: list[dict] = []
        self.uploads = 0

    def delivered(self, marker: str) -> list[str]:
        return [text for text, ok in self.posts if ok and marker in text]


class _SlackApiError(Exception):
    """A ``chat.postMessage`` answer of ``ok=false`` (Slack's definite refusal)."""

    def __init__(self, code: str):
        super().__init__(
            "The request to the Slack API failed. The server responded with: "
            f"{{'ok': False, 'error': '{code}'}}")
        self.response = {"ok": False, "error": code}


def _slack(posts: _PostLog, answers: list):
    """Slack adapter over the real ``send``/``send_image``; ``chat.postMessage`` answers ``answers``
    in order (an Exception raises, a dict is the reply)."""
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()

    async def _dm_target(chat_id, metadata):
        return chat_id

    adapter._dm_target = _dm_target
    queue = list(answers)

    async def _post_message(**kwargs):
        if not queue:
            raise AssertionError(f"unexpected extra chat.postMessage: {kwargs}")
        answer = queue.pop(0)
        posts.posts.append((str(kwargs.get("text", "")), not isinstance(answer, BaseException)))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    adapter._app.client.chat_postMessage = AsyncMock(side_effect=_post_message)

    async def _files_upload_v2(**kwargs):
        posts.uploads += len(kwargs.get("file_uploads") or [])
        return {"ok": True}

    adapter._app.client.files_upload_v2 = AsyncMock(side_effect=_files_upload_v2)
    return adapter


def _mattermost(posts: _PostLog, answers: list):
    """Mattermost adapter over the real ``send``/``_post_result``. POST ``/api/v4/posts`` answers
    ``(status, body)`` pairs in order — status ``None`` is a network failure — and, like ``_api``,
    records ``_last_post_status`` before returning ``{}``. Uploads are in-process."""
    import aiohttp

    from plugins.platforms.mattermost.adapter import MattermostAdapter

    adapter = MattermostAdapter(
        PlatformConfig(enabled=True, token="t", extra={"url": "https://mm.example.com"}))
    adapter._session = aiohttp.ClientSession()
    adapter._reply_mode = "channel"  # no thread-root resolution (that path is a GET)
    queue = list(answers)

    async def _api_post(path, payload):
        if not queue:
            raise AssertionError(f"unexpected extra post: {payload}")
        status, body = queue.pop(0)
        adapter._last_post_status = status
        adapter._last_post_error = "" if body else "network error"
        posts.posts.append((str(payload.get("message", "")), bool(body)))
        posts.payloads.append(payload)
        return body

    async def _upload_file(channel_id, file_data, filename, content_type="application/octet-stream"):
        posts.uploads += 1
        return f"fid{posts.uploads}"

    adapter._api_post = _api_post
    adapter._upload_file = _upload_file
    return adapter


def _script(platform: str, posts: _PostLog, answers: list):
    return _slack(posts, answers) if platform == "slack" else _mattermost(posts, answers)


def _refused_link_answer(platform: str, *, definite: bool):
    """One answer for the refused image's link text: ``definite`` = the platform said no and posted
    nothing; otherwise the request may have landed (reset / 5xx / no answer)."""
    if platform == "slack":
        return _SlackApiError("channel_not_found") if definite else ConnectionResetError(
            "connection reset by peer")
    return (400, {}) if definite else (503, {})


def _accepted(platform: str):
    return {"ok": True, "ts": "2.0"} if platform == "slack" else (201, {"id": "p2"})


def _link_retry_answers(platform: str, *, definite: bool, batch: bool) -> list:
    """Answers in call order: the link post, then (Mattermost only) the batch post, then the
    re-attempt of the link."""
    answers = [_refused_link_answer(platform, definite=definite)]
    if batch:
        answers.append(_accepted(platform))
    if definite:
        answers.append(_accepted(platform))
    return answers


async def _close(adapter):
    session = getattr(adapter, "_session", None)
    if session is not None:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_refused_link_is_delivered_when_its_post_was_definitely_refused(image_host, platform):
    """The sibling upload posts fine, so the batch succeeds — the refused link must still arrive."""
    posts = _PostLog()
    refused = f"{image_host}/huge.png?size={HUGE}"
    fine = f"{image_host}/ok.png?size={CAP // 2}"
    adapter = _script(platform, posts, _link_retry_answers(platform, definite=True, batch=True))
    try:
        result = await adapter.send_multiple_images("C1", [(refused, "too big"), (fine, "fine")])
    finally:
        await _close(adapter)

    assert result.success is True
    assert len(posts.delivered("huge.png")) == 1, f"the refused link never arrived: {posts.posts}"
    assert posts.posts[-1][1] is True, f"the link was not re-attempted: {posts.posts}"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_possibly_delivered_link_is_not_re_sent_and_is_reported(image_host, platform):
    """A reset/5xx link post may already be in the chat: no second copy, and the caller is told."""
    posts = _PostLog()
    refused = f"{image_host}/huge.png?size={HUGE}"
    fine = f"{image_host}/ok.png?size={CAP // 2}"
    adapter = _script(platform, posts, _link_retry_answers(platform, definite=False, batch=True))
    try:
        result = await adapter.send_multiple_images("C1", [(refused, "too big"), (fine, "fine")])
    finally:
        await _close(adapter)

    assert len(posts.posts) == (1 if platform == "slack" else 2), f"link re-sent: {posts.posts}"
    assert not posts.delivered("huge.png"), f"a possibly-posted link was re-sent: {posts.posts}"
    assert (result.raw_response or {}).get("delivery_uncertain") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_an_uncertain_link_alone_is_not_reported_as_delivered(image_host, platform):
    """Nothing was confirmed, so the aggregate must not claim success."""
    posts = _PostLog()
    refused = f"{image_host}/huge.png?size={HUGE}"
    adapter = _script(platform, posts, _link_retry_answers(platform, definite=False, batch=False))
    try:
        result = await adapter.send_multiple_images("C1", [(refused, "too big")])
    finally:
        await _close(adapter)

    assert result.success is False
    assert (result.raw_response or {}).get("delivery_uncertain") is True


@pytest.mark.asyncio
async def test_slack_still_chunks_at_ten_files_per_upload(tmp_path):
    posts = _PostLog()
    adapter = _slack(posts, [])
    paths = []
    for i in range(11):
        p = tmp_path / f"img_{i}.png"
        p.write_bytes(PNG)
        paths.append(str(p))

    result = await adapter.send_multiple_images("C1", [(f"file://{p}", "") for p in paths])

    assert result.success is True
    uploads = adapter._app.client.files_upload_v2.await_args_list
    assert [len(c.kwargs["file_uploads"]) for c in uploads] == [10, 1]


@pytest.mark.asyncio
async def test_mattermost_still_chunks_at_five_file_ids_per_post(tmp_path):
    posts = _PostLog()
    adapter = _mattermost(posts, [(201, {"id": "p1"}), (201, {"id": "p2"})])
    paths = []
    for i in range(6):
        p = tmp_path / f"img_{i}.png"
        p.write_bytes(PNG)
        paths.append(str(p))
    try:
        result = await adapter.send_multiple_images("ch", [(f"file://{p}", "") for p in paths])
    finally:
        await _close(adapter)

    assert result.success is True
    assert posts.uploads == 6
    assert [len(p.get("file_ids") or []) for p in posts.payloads] == [5, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_failed_batch_still_falls_back_per_image_without_duplicating_the_link(image_host, platform):
    """Control: a failed batch keeps the existing per-image fallback, and the already-posted link
    is not sent twice."""
    posts = _PostLog()
    refused = f"{image_host}/huge.png?size={HUGE}"
    fine = f"{image_host}/ok.png?size={CAP // 2}"
    per_image: list[str] = []

    async def _send_image(chat_id, image_url, caption=None, **_kwargs):
        per_image.append(image_url)
        return SendResult(success=True, message_id="img")

    adapter = _script(platform, posts, [_accepted(platform)])
    adapter.send_image = _send_image

    if platform == "slack":
        adapter._app.client.files_upload_v2 = AsyncMock(side_effect=RuntimeError("batch post failed"))
    else:
        calls = {"n": 0}

        async def _post_then_boom(path, payload):  # the link posts, the batch post fails
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("batch post failed")
            posts.posts.append((str(payload.get("message", "")), True))
            posts.payloads.append(payload)
            return {"id": "p1"}

        adapter._api_post = _post_then_boom
    try:
        result = await adapter.send_multiple_images("C1", [(refused, "too big"), (fine, "fine")])
    finally:
        await _close(adapter)

    assert result.success is True
    assert len(posts.delivered("huge.png")) == 1, f"link duplicated: {posts.posts}"
    assert per_image == [fine], f"the unposted image was not retried: {per_image}"
