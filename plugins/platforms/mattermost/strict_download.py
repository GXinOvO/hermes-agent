"""Use one HTTP request for a strict remote image download.

The normal aiohttp session may retry idempotent requests after disconnects;
this scoped httpx client has no transport retries or redirect resubmissions.
"""
import httpx
import logging

logger = logging.getLogger(__name__)


async def download(url):
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content, response.headers.get('content-type', 'application/octet-stream').split(';')[0]
    except httpx.HTTPError:
        logger.exception("Mattermost strict image download failed")
        return None
