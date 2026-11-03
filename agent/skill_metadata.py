"""Bounded, content-keyed reuse of parsed skill YAML (never filesystem freshness)."""

from copy import deepcopy
from functools import lru_cache


@lru_cache(maxsize=1024)
def _parsed_header(yaml_content: str) -> dict:
    # Import at call time: skill_utils owns the loader and its test/observer seam.
    from agent.skill_utils import yaml_load

    parsed = yaml_load(yaml_content)
    return parsed if isinstance(parsed, dict) else {}


def parse_skill_header(yaml_content: str) -> dict:
    """Parse the exact header bytes once; callers own all nested values they receive.

    The key excludes the body so a long skill does not occupy the cache twice for
    catalog's 4000-character preview and a full view. Readers still open each file
    on every lookup: same-size edits, restored timestamps and other processes are
    observed without a TTL, watcher or a weaker file signature.
    """
    return deepcopy(_parsed_header(yaml_content))
