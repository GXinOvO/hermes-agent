"""Fresh source collection shared by a catalog scan and its cache identity."""

import logging

logger = logging.getLogger("tools.skills_tool")


def collect_catalog_sources(roots):
    """Read each discoverable file once, retaining exactly the content being parsed.

    Directory membership is re-enumerated on every call. Project files still pass
    the quarantine iterator; no cache can turn a hidden project file into a row.
    Keeping the catalog's 4000-character preview in the identity avoids a
    stat/hash/read gap. Full-body identity decisions still run in the resolver.
    """
    from agent.skill_utils import (
        EXCLUDED_SKILL_DIRS, TIER_PROJECT, iter_project_skill_files, iter_skill_index_files,
    )
    from tools import skills_tool as st

    sources = []
    for tier, root in roots:
        files = (iter_project_skill_files(root) if tier == TIER_PROJECT
                 else iter_skill_index_files(root, "SKILL.md"))
        for path in files:
            if any(part in EXCLUDED_SKILL_DIRS for part in path.parts):
                continue
            try:
                content = st._read_skill_text(path)[:4000]
            except Exception as exc:
                logger.debug("Failed to read skill file %s: %s", path, exc, exc_info=True)
                continue
            sources.append((tier, root, path, content))
    return sources


def scan_catalog_sources(sources):
    """Apply the existing catalog metadata preview rules."""
    from tools import skills_tool as st

    scanned = []
    for tier, root, path, content in sources:
        try:
            frontmatter, body = st._parse_frontmatter(content[:4000])
            description = frontmatter.get("description", "")
            if not description:
                description = next((ln for ln in map(str.strip, body.strip().split("\n"))
                                    if ln and not ln.startswith("#")), description)
            scanned.append({
                "name": frontmatter.get("name", path.parent.name)[:st.MAX_NAME_LENGTH],
                "description": st._truncate_description(description),
                "category": st._get_category_from_path(path), "tier": tier, "root": root,
                "path": path, "_frontmatter": frontmatter,
            })
        except Exception as exc:
            logger.debug("Skipping skill at %s: failed to parse: %s", path, exc, exc_info=True)
    return scanned


def visible_catalog_rows(rows):
    """Recheck live offer-time gates; preserve per-file parse/gate failure recovery."""
    from tools import skills_tool as st

    scanned = []
    for row in rows:
        try:
            entry = dict(row)
            frontmatter = entry.pop("_frontmatter")
            entry["visible"] = bool(st.skill_matches_platform(frontmatter)
                                    and st.skill_matches_environment(frontmatter)
                                    and st.skill_matches_apps(frontmatter))
            scanned.append(entry)
        except Exception as exc:
            logger.debug("Skipping skill at %s: failed to filter: %s", row["path"], exc, exc_info=True)
    return scanned
