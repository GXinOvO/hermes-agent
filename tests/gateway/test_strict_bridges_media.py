"""Every supported media method, upload stage, and uncertain transport boundary."""
import asyncio
from contextlib import suppress
import json
import sqlite3

import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway.test_strict_bridges_http import connected, http_peer, target


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'matrix', 'qqbot'])
@pytest.mark.parametrize('method', ['send_image_file', 'send_voice', 'send_video'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_each_local_media_method_stops_on_first_unknown(kind, method, fault, tmp_path, monkeypatch):
    from PIL import Image
    path = tmp_path / 'asset.png'
    Image.new('RGB', (4, 4), (2, 3, 4)).save(path)
    async with http_peer(fault) as wire, connected(kind, wire, monkeypatch) as adapter:
        if kind == 'line':
            adapter.public_base_url = 'https://local.invalid'
        key = {'send_image_file':'image_path', 'send_voice':'audio_path', 'send_video':'video_path'}[method]
        kwargs = {'chat_id':target(kind), key:str(path)}
        if kind == 'matrix' and method == 'send_voice':
            kwargs['is_voice'] = False
        result = await strict_call(adapter, method, **kwargs)
        assert result.success is (fault is None), result
        expected = {'line':1, 'matrix':2, 'qqbot':5}[kind] if not fault else 1
        assert len(wire.events) == expected, wire.events


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize('fault', ['lost', '503'])
async def test_qq_local_file_each_stage_never_retries_or_continues(stage, fault, tmp_path, monkeypatch):
    path = tmp_path / 'sealed.txt'
    path.write_text('exact sealed bytes')
    async with http_peer(fault if stage else None, stage) as wire, connected('qqbot', wire, monkeypatch) as adapter:
        result = await strict_call(adapter, 'send_document', chat_id='peer', file_path=str(path))
        assert result.success is (stage == 0), result
        assert len(wire.events) == (stage or 5)
        paths = [p for _, p, _ in wire.events]
        assert paths == ['/v2/users/peer/upload_prepare', '/cos/1', '/v2/users/peer/upload_part_finish',
                         '/v2/users/peer/files', '/v2/users/peer/messages'][:stage or 5]
        if stage == 0:
            assert wire.events[1][2] == b'exact sealed bytes'


@pytest.mark.asyncio
async def test_qq_strict_requires_account_bound_durable_route(tmp_path, monkeypatch):
    from plugins.platforms.qqbot.routes import ChatRoutes
    async with http_peer() as wire, connected('qqbot', wire, monkeypatch) as adapter:
        adapter._chat_type_map['unknown'] = 'group'
        result = await strict_call(adapter, chat_id='unknown', content='answer')
        assert not result.success and not wire.events
        ChatRoutes(adapter._app_id).remember('group-peer', 'group')
        result = await strict_call(adapter, chat_id='group-peer', content='answer')
        assert result.success
        assert wire.events[0][1] == '/v2/groups/group-peer/messages'
        # Same target string in another QQ app cannot borrow the route.
        adapter._app_id = 'different-account'
        result = await strict_call(adapter, chat_id='group-peer', content='answer')
        assert not result.success and len(wire.events) == 1


def test_qq_route_schema_and_fresh_instance(tmp_path, monkeypatch):
    from plugins.platforms.qqbot.routes import ChatRoutes
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    first = ChatRoutes('account-a')
    first.remember('group', 'group')
    assert ChatRoutes('account-a').get('group') == 'group'
    assert ChatRoutes('account-b').get('group') is None
    first.remember('group', 'guild')
    assert ChatRoutes('account-a').get('group') == 'conflict'
    first.remember('group', 'group')
    assert ChatRoutes('account-a').get('group') == 'conflict'
    with sqlite3.connect(first.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        conn.execute('PRAGMA user_version=2')
    with pytest.raises(ValueError, match='newer'):
        ChatRoutes('account-a').get('group')


@pytest.mark.asyncio
async def test_matrix_e2ee_fails_before_any_request(monkeypatch):
    async with http_peer() as wire, connected('matrix', wire, monkeypatch) as adapter:
        adapter._encryption = True
        result = await strict_call(adapter, chat_id='!room:local', content='answer')
        assert not result.success and 'unverified' in result.error
        assert not wire.events


@pytest.mark.asyncio
async def test_line_https_url_image_is_native_one_call(monkeypatch):
    async with http_peer() as wire, connected('line', wire, monkeypatch) as adapter:
        result = await strict_call(adapter, 'send_image', chat_id='Upeer', image_url='https://local.invalid/asset.png')
        assert result.success and len(wire.events) == 1
        assert json.loads(wire.events[0][2])['messages'][0]['type'] == 'image'


@pytest.mark.asyncio
async def test_line_complete_five_bubbles_no_silent_truncation(monkeypatch):
    from plugins.platforms.line.adapter import LINE_PER_BUBBLE_CHARS
    text = 'x' * (LINE_PER_BUBBLE_CHARS * 5)
    async with http_peer() as wire, connected('line', wire, monkeypatch) as adapter:
        result = await strict_call(adapter, chat_id='Upeer', content=text)
        assert result.success
        messages = json.loads(wire.events[0][2])['messages']
        assert len(messages) == 5 and ''.join(m['text'] for m in messages) == text
        result = await strict_call(adapter, chat_id='Upeer', content=text + 'x')
        assert not result.success and len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'matrix', 'qqbot'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_valid_audio_and_video_payloads_use_original_media_paths(kind, fault, tmp_path, monkeypatch):
    import shutil
    import subprocess
    import wave
    ffmpeg = shutil.which('ffmpeg')
    assert ffmpeg, 'This proof requires the real ffmpeg binary'
    wav = tmp_path / 'voice.wav'
    with wave.open(str(wav), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    video = tmp_path / 'video.mp4'
    made = await asyncio.to_thread(subprocess.run,
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=16x16:d=0.1',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)],
        capture_output=True, text=True, check=False, timeout=30)
    assert made.returncode == 0, made.stderr
    for method, key, path in [('send_voice', 'audio_path', wav), ('send_video', 'video_path', video)]:
        async with http_peer(fault) as wire, connected(kind, wire, monkeypatch) as adapter:
            if kind == 'line':
                adapter.public_base_url = 'https://local.invalid'
            result = await strict_call(adapter, method, **{'chat_id':target(kind), key:str(path)})
            assert result.success is (fault is None), result
            assert len(wire.events) == (1 if fault else {'line':1, 'matrix':2, 'qqbot':5}[kind])
            if kind == 'matrix' and not fault:
                message = json.loads(wire.events[-1][2])
                assert message['msgtype'] == ('m.audio' if method == 'send_voice' else 'm.video')
                if method == 'send_voice':
                    assert 'org.matrix.msc3245.voice' in message
                    assert message['info']['mimetype'] in {'audio/ogg', 'audio/opus'}


@pytest.mark.asyncio
async def test_irc_raw_tcp_human_write_has_no_acceptance_ack():
    from plugins.platforms.irc.adapter import IRCAdapter
    got = asyncio.Event()
    wire = []

    async def receive(reader, writer):
        wire.append(await reader.readline())
        got.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    adapter = IRCAdapter(PlatformConfig(enabled=True))
    async with server:
        reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
        adapter._reader, adapter._writer = reader, writer
        try:
            with strict_transport_scope():
                result = await adapter.send('#local', 'sealed answer')
            assert not result.success and not wire
            result = await adapter.send('#local', 'human answer')
            await asyncio.wait_for(got.wait(), timeout=2)
            assert result.success and wire == [b'PRIVMSG #local :human answer\r\n']
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
