from copy import deepcopy

import pytest

from cron.strict_thread_delivery import digest, validate
from plugins.platforms.frozen_delivery import digest as byte_digest, encode
from plugins.platforms.weixin import frozen_delivery as helper
from tests.gateway import frozen_live_http_peer as native


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'home', 'execution', 'route', 'envelope', 'redirect', 'extra', 'media', 'slot'])
async def test_weixin_saved_binding_rejects_bad_crossfields_before_mutation(tmp_path, monkeypatch, fault):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    media = tmp_path / 'proof.bin'
    media.write_bytes(b'ORIGINAL')
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        state = native.state_for('weixin', chat)
        state.profile = str(tmp_path)
        state.execution_id = 'trusted-execution'
        state.target = {'platform': 'weixin', 'chat_id': chat, 'thread_id': None}
        plan = await helper.freeze(adapter, state, [('send_document', {'file_path': str(media)})], [(str(media), False)])
        target = {'kind': 'live-http', 'route': state.target, 'plan': deepcopy(plan['binding'])}
        binding = target['plan']
        changes = {
            'home': lambda: binding['identity'].update(home=str(tmp_path / 'different'), scope=str(tmp_path / 'different')),
            'execution': lambda: binding['identity'].update(execution='different-execution'),
            'route': lambda: binding['identity']['target'].update(chat_id='different-peer'),
            'envelope': lambda: binding['crypto_context'].update(nonce='YQ=='),
            'redirect': lambda: binding['prepared'][0].update(redirects=True),
            'extra': lambda: binding.update(untrusted_field=True),
            'media': lambda: binding['media'][0].__setitem__(3, 'wrong-basename.bin'),
            'slot': lambda: binding['prepared'][0]['response_slot'].update(path='/different/upload'),
        }
        if fault in changes:
            changes[fault]()
        binding['transport_sha256'] = byte_digest(encode({key: value for key, value in binding.items() if key != 'transport_sha256'}))
        bundle = {'version': 2, 'home': str(tmp_path), 'job': {'execution_id': 'trusted-execution'},
                  'content_sha256': 'a' * 64, 'for_failure': False, 'targets': [target]}
        value = {'binding': bundle, 'binding_sha256': digest(bundle),
                 'items': [{'stage': 'initial', 'receipt': None, 'receipt_sha256': None}]}
        if fault == 'none':
            assert validate(value) == value
        else:
            with pytest.raises((ValueError, TypeError, KeyError)):
                validate(value)
        assert wire.events == []
