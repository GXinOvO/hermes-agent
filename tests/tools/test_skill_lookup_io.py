"""Skill lookup I/O stays single-pass and header-bounded."""

import io
import os
from collections import Counter
from pathlib import Path

import pytest

from tools import skills_tool as st


def _write_skill(root: Path, relative: str, name: str, body: str = "body") -> Path:
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: fixture\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_candidate_lookup_uses_one_directory_walk_for_modern_and_legacy(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    for index in range(5):
        _write_skill(root, f"category/modern-{index}", f"modern-{index}")
    legacy = root / "nested" / "legacy" / "target.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy body", encoding="utf-8")

    visits = Counter()
    real_scandir = os.scandir

    def observe_scandir(path):
        candidate = Path(path) if isinstance(path, (str, os.PathLike)) else None
        if candidate is not None and candidate.is_relative_to(root):
            visits[str(candidate)] += 1
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", observe_scandir)
    candidates = st._collect_skill_candidates("target", None, [root])

    assert [(skill_dir, skill_md) for skill_dir, skill_md in candidates] == [(None, legacy)]
    assert visits and max(visits.values()) == 1


def test_candidate_lookup_reads_only_frontmatter_header_for_nonmatching_bodies(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    body = "x" * 100_000
    skill_paths = [
        _write_skill(root, f"category/modern-{index}", f"modern-{index}", body)
        for index in range(6)
    ]
    target = root / "target.md"
    target.write_text("target body", encoding="utf-8")

    read_bytes = Counter()
    real_open = io.open

    class CountingFile:
        def __init__(self, wrapped, path):
            self._wrapped = wrapped
            self._path = path

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self._wrapped.__exit__(*args)

        def read(self, *args, **kwargs):
            data = self._wrapped.read(*args, **kwargs)
            read_bytes[self._path] += len(data.encode("utf-8") if isinstance(data, str) else data)
            return data

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def observe_open(file, *args, **kwargs):
        wrapped = real_open(file, *args, **kwargs)
        path = Path(file) if isinstance(file, (str, os.PathLike)) else None
        if path is not None and path in skill_paths:
            return CountingFile(wrapped, path)
        return wrapped

    monkeypatch.setattr(io, "open", observe_open)
    candidates = st._collect_skill_candidates("target", None, [root])

    assert [(skill_dir, skill_md) for skill_dir, skill_md in candidates] == [(None, target)]
    assert set(read_bytes) == set(skill_paths)
    assert sum(read_bytes.values()) < len(body) * len(skill_paths) // 4


def test_lookup_preserves_legacy_recursive_glob_symlink_boundary(tmp_path):
    root = tmp_path / "skills"
    real = root / "category" / "target.md"
    real.parent.mkdir(parents=True)
    real.write_text("real legacy")
    foreign = tmp_path / "external"
    foreign.mkdir()
    (foreign / "target.md").write_text("foreign legacy")
    _write_skill(foreign, "modern", "modern-alias")
    try:
        (root / "linked-category").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    candidates = st._collect_skill_candidates("target", None, [root])
    assert candidates == [(None, real)]
    modern = st._collect_skill_candidates("modern-alias", None, [root])
    assert len(modern) == 1
    assert modern[0][1].resolve() == foreign / "modern" / "SKILL.md"


def test_lookup_does_not_offer_excluded_skill_md_as_legacy(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, ".git/hidden", "SKILL")
    assert st._collect_skill_candidates("SKILL", None, [root]) == []


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_frontmatter_reader_matches_original_text_for_nonstandard_lines(tmp_path, newline):
    from tools.skills_tool_plugin import _safe_frontmatter, _read_skill_text

    path = tmp_path / "SKILL.md"
    text = "---" + newline + "description: fixture" + newline + "padding: " + "x" * 5000 + newline + "name: late-alias" + newline + "---" + newline + "body"
    path.write_bytes(text.encode())
    expected = st._parse_frontmatter(_read_skill_text(path))[0]
    assert expected.get("name") == "late-alias"
    assert _safe_frontmatter(path) == expected
