"""SMTP DATA acceptance must never trigger strict attachment fallback."""
from email import policy
from email.parser import BytesParser

import pytest

from gateway.strict_transport import strict_transport_scope, strict_call
from tests.gateway import test_strict_http_peers as peers


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_smtp_batch_lost_ack(strict, tmp_path):
    paths = [tmp_path / f'{i}.png' for i in range(2)]
    for p in paths:
        p.write_bytes(b'image')
    images = [(p.as_uri(), '') for p in paths]
    async with peers.smtp_peer(drop_first=True) as peer:
        adapter = peers.email_for(peer)
        if strict:
            with strict_transport_scope():
                result = await adapter.send_multiple_images('receiver@inert.invalid', images)
        else:
            result = await adapter.send_multiple_images('receiver@inert.invalid', images)
        assert result.success is (not strict)
        assert len(peer.messages) == (1 if strict else 3)
        first = BytesParser(policy=policy.default).parsebytes(peer.messages[0])
        assert len(list(first.iter_attachments())) == 2


@pytest.mark.asyncio
async def test_smtp_success_keeps_all_attachments(tmp_path):
    paths = [tmp_path / f'{i}.png' for i in range(3)]
    for p in paths:
        p.write_bytes(b'image')
    async with peers.smtp_peer() as peer:
        adapter = peers.email_for(peer)
        result = await strict_call(adapter, 'send_multiple_images', chat_id='receiver@inert.invalid',
                                   images=[(p.as_uri(), '') for p in paths])
        assert result.success and len(peer.messages) == 1
        msg = BytesParser(policy=policy.default).parsebytes(peer.messages[0])
        assert [p.get_payload(decode=True) for p in msg.iter_attachments()] == [b'image'] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize('method, kwargs', [('send', {'content': 'text'}), ('send_image', {'image_url': 'https://inert.invalid/image.png'}),
    ('send_document', {'file_path': 'PLACEHOLDER'})])
async def test_smtp_text_and_single_media_one_data_after_lost_ack(method, kwargs, tmp_path):
    local = tmp_path / 'document.txt'
    local.write_text('document')
    kwargs = {k: str(local) if v == 'PLACEHOLDER' else v for k, v in kwargs.items()}
    async with peers.smtp_peer(drop_first=True) as peer:
        result = await strict_call(peers.email_for(peer), method, chat_id='receiver@inert.invalid', **kwargs)
        assert not result.success and len(peer.messages) == 1


@pytest.mark.asyncio
async def test_smtp_incomplete_batch_is_failure_before_data(tmp_path):
    path = tmp_path / 'exists.png'
    path.write_bytes(b'image')
    async with peers.smtp_peer() as peer:
        with strict_transport_scope():
            result = await peers.email_for(peer).send_multiple_images('receiver@inert.invalid',
                [(path.as_uri(), ''), ((tmp_path / 'missing.png').as_uri(), '')])
        assert not result.success and peer.messages == []
