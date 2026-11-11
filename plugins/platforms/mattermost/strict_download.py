"""Use one HTTP request for a strict remote image download.

The normal aiohttp session may retry idempotent requests after disconnects;
this scoped httpx client has no transport retries or redirect resubmissions.
"""
import httpx
import logging

logger = logging.getLogger(__name__)


async def download(url):
    from gateway.platforms.base import read_httpx_body_with_limit
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            async with client.stream('GET', url) as response:
                response.raise_for_status()
                return (await read_httpx_body_with_limit(response, media_type='image'),
                        response.headers.get('content-type', 'application/octet-stream').split(';')[0])
    except (httpx.HTTPError, ValueError):
        logger.exception("Mattermost strict image download failed")
        return None
