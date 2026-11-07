"""A real fresh-process rebuild; stdin only, no private keys in artifacts."""
import asyncio
import json
import sys
from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.weixin.adapter import WeixinAdapter
from plugins.platforms.weixin.frozen_delivery import freeze


async def main():
    value = json.load(sys.stdin)
    adapter = WeixinAdapter(PlatformConfig())
    adapter._base_url = value['base']
    adapter._cdn_base_url = value['cdn']
    adapter._account_id = 'account-A'
    adapter._token = 'synthetic-weixin-A'
    adapter._token_store._cache[adapter._token_store._key('account-A', 'peer-A')] = 'synthetic-context-A'
    state = SimpleNamespace(**value['state'])
    plan = await freeze(adapter, state, value['calls'], value['retained'], saved_binding=value['binding'])
    print(json.dumps({'binding': plan['binding'], 'mutations': 0}))


if __name__ == '__main__':
    asyncio.run(main())
