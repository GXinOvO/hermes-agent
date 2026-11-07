import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.cron.strict_fanout_fixture import consume


if __name__ == '__main__':
    with patch('cron.strict_native_delivery.legacy_target', return_value=True):
        asyncio.run(consume(*sys.argv[1:]))
