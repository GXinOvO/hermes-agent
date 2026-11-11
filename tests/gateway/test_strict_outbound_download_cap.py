import pytest

from plugins.platforms.mattermost.strict_download import download as mattermost_download
from plugins.platforms.slack.strict_delivery import source_bytes
from tests.gateway.test_outbound_image_download_cap import CAP, HUGE, _ChunkedImage

pytest_plugins = ('tests.gateway.test_outbound_image_download_cap',)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'mattermost'])
async def test_strict_remote_download_respects_cap_without_buffering_entire_body(image_host, platform):
    download = source_bytes if platform == 'slack' else mattermost_download

    small = await download(f'{image_host}/small.png?size={CAP // 2}')
    assert small is not None and len(small[0]) == CAP // 2

    huge_path = f'/strict-huge-{platform}.png?size={HUGE}'
    finished = _ChunkedImage.expect(huge_path)
    if platform == 'slack':
        with pytest.raises(ValueError, match='too large'):
            await download(image_host + huge_path)
    else:
        assert await download(image_host + huge_path) is None
    assert finished.wait(10)
    assert _ChunkedImage.served[huge_path] < HUGE // 2