"""Frozen local media bytes and explicit unsupported branch preflight."""
import hashlib
import json
from pathlib import Path

import pytest

from tests.gateway import strict_native_thread_support as native


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram'])
@pytest.mark.parametrize('method,key,suffix', [('send_document', 'file_path', '.bin'),
    ('send_image_file', 'image_path', '.png'), ('send_video', 'video_path', '.mp4')])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_frozen_media_bytes_and_filename_ignore_path_replacement(name, method, key, suffix, fault,
                                                                      tmp_path, monkeypatch, record_property):
    path = tmp_path / ('source' + suffix)
    path.write_bytes(b'original frozen bytes')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    async with native.peer(name, [fault]) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        calls = [(method, {key: str(path), 'caption': 'frozen caption'})]
        plan = helper.freeze(adapter, state, calls, [(str(path), False)], new_thread=False)
        assert plan['binding']['media'] == [[str(path), False, digest, path.name]]
        path.write_bytes(b'changed after freeze')
        calls[0][1]['caption'] = 'mutated caption'
        path.unlink()
        if fault:
            with pytest.raises(Exception):
                await helper.send(plan)
        else:
            assert (await helper.send(plan))['ok'] is True
            media = [value for value in wire.events[0]['fields'].values() if isinstance(value, dict) and 'sha256' in value]
            assert media == [{'sha256': digest, 'filename': path.name}]
            assert 'frozen caption' in json.dumps(wire.events[0]) and 'mutated caption' not in json.dumps(wire.events[0])
        assert len(wire.events) == 1
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram'])
async def test_later_missing_and_aba_media_refuse_entire_new_thread(name, tmp_path, monkeypatch):
    path = tmp_path / 'first.bin'
    path.write_bytes(b'first')
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        calls = [('send', {'content': 'first'}), ('send_document', {'file_path': str(tmp_path / 'missing')})]
        with pytest.raises((OSError, RuntimeError, ValueError)):
            helper.freeze(adapter, state, calls, [(str(tmp_path / 'missing'), False)], new_thread=True)
        calls = [('send_document', {'file_path': str(path)})]
        real_open = Path.open
        def changed_open(self, *args, **kwargs):
            if self == path and args and args[0] == 'rb':
                replacement = self.with_suffix('.replacement')
                with real_open(replacement, 'wb') as stream:
                    stream.write(b'changed')
                replacement.replace(self)
                with real_open(replacement, 'wb') as stream:
                    stream.write(b'first')
                replacement.replace(self)
            return real_open(self, *args, **kwargs)
        with path.open('rb') as original, monkeypatch.context() as patch:
            assert original.fileno() >= 0
            patch.setattr(Path, 'open', changed_open)
            with pytest.raises((RuntimeError, ValueError), match='changed'):
                helper.freeze(adapter, state, calls, [(str(path), False)], new_thread=True)
        assert wire.events == []


@pytest.mark.asyncio
async def test_matrix_e2ee_and_media_refuse_before_root_mutation(tmp_path, monkeypatch):
    path = tmp_path / 'media.bin'
    path.write_bytes(b'inert')
    async with native.peer('matrix') as wire, native.connected('matrix', wire, monkeypatch) as (adapter, state):
        helper = native.module('matrix')
        adapter._encryption = True
        with pytest.raises((RuntimeError, ValueError), match='E2EE'):
            helper.freeze(adapter, state, [('send', {'content': 'body'})], [], new_thread=True)
        adapter._encryption = False
        with pytest.raises((RuntimeError, ValueError), match='media'):
            helper.freeze(adapter, state, [('send', {'content': 'body'}), ('send_document', {'file_path': str(path)})],
                          [(str(path), False)], new_thread=True)
        assert wire.events == []
