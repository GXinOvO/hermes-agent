"""Content reuse must preserve live skill lookup, catalog and prompt contracts."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from agent import prompt_builder as pb
from agent import skill_utils as su
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import skills_tool as st
from tools.registry import registry


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text("skills:\n  project_discovery: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    # Plugin discovery is an external source, not the on-disk lookup under test.
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    st.clear_skills_cache()
    pb.clear_skills_system_prompt_cache()
    yield home
    st.clear_skills_cache()
    pb.clear_skills_system_prompt_cache()


def _write(root, relative, name, description="fixture description", body="body-one"):
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        "metadata:\n  hermes:\n    tags: [fixture, nested]\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def _replace_preserving_mtime(path, before, after):
    original = path.read_bytes()
    updated = original.replace(before.encode(), after.encode())
    assert original != updated and len(original) == len(updated)
    stamp = path.stat()
    path.write_bytes(updated)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns


def _view(name, *, file_path=None, task_id=None):
    result = registry.dispatch(
        "skill_view", {"name": name, "file_path": file_path}, task_id=task_id
    )
    assert isinstance(result, str)
    return json.loads(result)


def _names():
    return {row["name"]: row["description"] for row in st.find_all_skills()}


def test_warm_consumers_reuse_unchanged_frontmatter(skill_home, monkeypatch):
    """Observe real YAML work without replacing any lookup or parsing result."""
    for number in range(4):
        _write(skill_home / "skills", f"category/folder-{number}", f"alias-{number}")
    parsed = []
    original = su.yaml_load

    def observe(content):
        if "fixture description" in content:
            parsed.append(content)
        return original(content)

    monkeypatch.setattr(su, "yaml_load", observe)
    for _ in range(2):
        result = _view("alias-0")
        assert result["success"] and result["name"] == "alias-0"
        assert len(_names()) == 4
        assert "alias-0: fixture description" in pb.build_skills_system_prompt()
    assert len(parsed) == 4, "unchanged headers should be parsed once across consumers"


def test_cold_catalog_walks_each_directory_once(skill_home, monkeypatch):
    for number in range(3):
        _write(skill_home / "skills", f"category/folder-{number}", f"alias-{number}")
    visits = Counter()
    original = os.scandir

    def observe(path):
        if isinstance(path, (str, os.PathLike)):
            candidate = Path(path)
            if candidate.is_relative_to(skill_home / "skills"):
                visits[str(candidate)] += 1
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    assert len(_names()) == 3
    assert visits and max(visits.values()) == 1, "signature and catalog must share one walk"


def test_catalog_sees_same_size_name_and_description_edit_immediately(skill_home):
    skill = _write(skill_home / "skills", "category/folder", "alias-one", "description-one")
    assert _names() == {"alias-one": "description-one"}
    _replace_preserving_mtime(skill, "alias-one", "alias-two")
    _replace_preserving_mtime(skill, "description-one", "description-two")
    assert _names() == {"alias-two": "description-two"}
    assert not _view("alias-one")["success"]
    assert _view("alias-two")["success"]
    assert "alias-two: description-two" in pb.build_skills_system_prompt()


def test_warm_lookup_rechecks_collision_add_delete_and_rename(skill_home):
    root = skill_home / "skills"
    first = _write(root, "a/one", "shared", body="first-body")
    assert _view("shared")["success"]
    second = _write(root, "b/two", "shared", body="other-body")
    collision = _view("shared")
    assert not collision["success"] and "Ambiguous skill name" in collision["error"]
    assert set(collision["load_names"]) == {"a/one", "b/two"}
    assert "first-body" in _view("a/one")["content"]
    assert set(_names()) == {"a/one", "b/two"}
    second.unlink()
    assert _view("shared")["success"]
    first.parent.rename(first.parent.with_name("renamed"))
    assert not _view("one")["success"]
    assert _view("renamed")["success"]
    assert set(_names()) == {"shared"}


def test_cached_lookup_preserves_tier_precedence(skill_home):
    root = skill_home / "skills"
    _write(root, "one", "shared", body="local-body")
    external = skill_home.parent / "external"
    _write(external, "two", "shared", body="external-body")
    (skill_home / "config.yaml").write_text(
        f"skills:\n  project_discovery: false\n  external_dirs: ['{external}']\n", encoding="utf-8"
    )
    assert "local-body" in _view("shared")["content"]
    assert "local-body" in _view("shared")["content"]
    (root / "one/SKILL.md").unlink()
    assert "external-body" in _view("shared")["content"]


def test_reference_edits_bypass_no_freshness_checks(skill_home):
    skill = _write(skill_home / "skills", "category/folder", "alias-one")
    reference = skill.parent / "references/guide.md"
    reference.parent.mkdir()
    reference.write_text("reference-one", encoding="utf-8")
    assert _view("alias-one", file_path="references/guide.md", task_id="reference")["content"] == "reference-one"
    _replace_preserving_mtime(reference, "reference-one", "reference-two")
    assert _view("alias-one", file_path="references/guide.md", task_id="reference")["content"] == "reference-two"
    _replace_preserving_mtime(skill, "body-one", "body-two")
    assert "body-two" in _view("alias-one")["content"]


def test_external_process_edits_are_seen_by_all_consumers(skill_home):
    skill = _write(skill_home / "skills", "category/folder", "alias-one", "description-one")
    assert _view("alias-one")["success"]
    assert "alias-one" in _names()
    assert "alias-one: description-one" in pb.build_skills_system_prompt()
    script = skill_home.parent / "edit_skill.py"
    script.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]); s=p.stat(); data=p.read_bytes()\n"
        "p.write_bytes(data.replace(b'alias-one',b'alias-two').replace(b'description-one',b'description-two'))\n"
        "os.utime(p,ns=(s.st_atime_ns,s.st_mtime_ns))\n", encoding="utf-8"
    )
    child = subprocess.run(
        [sys.executable, str(script), str(skill)], capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(skill_home.parent),
             "HERMES_HOME": str(skill_home), "TMPDIR": str(skill_home.parent)},
        timeout=20, check=False,
    )
    assert child.returncode == 0, child.stderr
    assert not _view("alias-one")["success"]
    assert _view("alias-two")["success"]
    assert _names() == {"alias-two": "description-two"}
    assert "alias-two: description-two" in pb.build_skills_system_prompt()


def test_profiles_do_not_share_catalog_or_lookup_state(skill_home):
    other = skill_home.parent / "other-profile"
    _write(skill_home / "skills", "folder", "shared", body="profile-one")
    _write(other / "skills", "folder", "shared", body="profile-two")
    for home, body in ((skill_home, "profile-one"), (other, "profile-two"), (skill_home, "profile-one")):
        token = set_hermes_home_override(str(home))
        try:
            assert body in _view("shared")["content"]
            assert set(_names()) == {"shared"}
            assert "shared: fixture description" in pb.build_skills_system_prompt()
        finally:
            reset_hermes_home_override(token)


def test_header_cache_returns_independent_nested_values(skill_home):
    skill = _write(skill_home / "skills", "folder", "shared")
    first = st._safe_frontmatter(skill)
    first["metadata"]["hermes"]["tags"].append("caller-mutation")
    assert st._safe_frontmatter(skill)["metadata"]["hermes"]["tags"] == ["fixture", "nested"]


def test_flat_legacy_and_symlink_lookup_remain_live(skill_home):
    root = skill_home / "skills"
    skill = _write(root, "real/folder", "declared")
    flat = root / "legacy.md"
    flat.write_text("legacy-one", encoding="utf-8")
    assert _view("legacy")["content"] == "legacy-one"
    _replace_preserving_mtime(flat, "legacy-one", "legacy-two")
    assert _view("legacy")["content"] == "legacy-two"
    link = root / "linked"
    try:
        link.symlink_to(skill.parent, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    assert _view("linked")["success"]
    assert _view("declared")["success"]
    _replace_preserving_mtime(skill, "declared", "replaced")
    assert not _view("declared")["success"]
    assert _view("replaced")["success"]
