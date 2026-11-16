import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/update_fork_catalog.py"
SPEC = importlib.util.spec_from_file_location("catalog_snapshots_subject", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def repository(root):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    git("init", "-q", "-b", "dev")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    catalog = root / "FORK_FEATURES.md"
    catalog.write_text("# Catalog\n| F01 | old |\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "baseline")
    catalog.write_text(updater.set_checkpoint(catalog.read_text(), git("rev-parse", "HEAD")), encoding="utf-8")
    (root / "feature.py").write_text("value = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "feature")
    return catalog, git


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("change", ["content", "checkpoint", "newline"])
def test_working_catalog_changes_stop_before_review(tmp_path, monkeypatch, cached, change):
    catalog, git = repository(tmp_path)
    cache = tmp_path / "cache"
    if cached:
        monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(cache))
    else:
        monkeypatch.delenv("FORK_CATALOG_CACHE_DIR", raising=False)
    source = catalog.read_text()
    if change == "content":
        source = source.replace("| F01 | old |", "| F01 | uncommitted |")
    elif change == "checkpoint":
        source = updater.set_checkpoint(source, git("rev-parse", "HEAD"))
    else:
        source += "\n"
    catalog.write_text(source, encoding="utf-8")
    before = catalog.read_bytes()
    with patch.object(updater, "request_edits", return_value={"edits": []}) as request:
        with pytest.raises(ValueError, match="working catalog differs"):
            updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        request.assert_not_called()
    assert catalog.read_bytes() == before
    assert not cache.exists()


def test_committed_catalog_changes_are_reviewed_from_current_snapshot(tmp_path, monkeypatch):
    catalog, git = repository(tmp_path)
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    catalog.write_text(catalog.read_text().replace("| F01 | old |", "| F01 | maintained |"), encoding="utf-8")
    git("add", "FORK_FEATURES.md")
    git("commit", "-qm", "catalog maintenance without checkpoint advance")
    target = git("rev-parse", "HEAD")
    response = {"edits": [{"old": "| F01 | maintained |", "new": "| F01 | reviewed |"}]}
    with patch.object(updater, "request_edits", return_value=response) as request:
        assert updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    assert "| F01 | maintained |" in request.call_args.args[-1]["catalog"]
    assert "| F01 | reviewed |" in catalog.read_text()
    assert updater.checkpoint(catalog.read_text()) == target


def test_publication_then_new_code_preserves_published_catalog(tmp_path, monkeypatch):
    catalog, git = repository(tmp_path)
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    response = {"edits": [{"old": "| F01 | old |", "new": "| F01 | published |"}]}
    with patch.object(updater, "request_edits", return_value=response):
        assert updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    git("add", "FORK_FEATURES.md")
    git("commit", "-qm", "catalog publication")
    with patch.object(updater, "request_edits") as idle:
        assert not updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        idle.assert_not_called()
    (tmp_path / "feature.py").write_text("value = 2\n", encoding="utf-8")
    git("add", "feature.py")
    git("commit", "-qm", "next feature")
    target = git("rev-parse", "HEAD")
    with patch.object(updater, "request_edits", return_value={"edits": []}) as request:
        assert updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    assert "| F01 | published |" in request.call_args.args[-1]["catalog"]
    assert updater.checkpoint(catalog.read_text()) == target


def test_changed_committed_catalog_invalidates_pending_plan(tmp_path, monkeypatch):
    catalog, git = repository(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(cache))
    original = catalog.read_bytes()
    with patch.object(updater, "request_edits", side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError, match="offline"):
            updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    assert catalog.read_bytes() == original
    old_target = git("rev-parse", "HEAD")
    catalog.write_text(catalog.read_text().replace("| F01 | old |", "| F01 | maintained |"), encoding="utf-8")
    git("add", "FORK_FEATURES.md")
    git("commit", "-qm", "committed catalog changes invalidate old plan")
    target = git("rev-parse", "HEAD")
    with patch.object(updater, "request_edits", return_value={"edits": []}) as request:
        assert updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    assert request.call_args.args[-1]["target"] == target
    assert target != old_target
    assert updater.checkpoint(catalog.read_text()) == target
    assert len(list((cache / "plans").glob("*.json"))) == 2


def test_latest_preservation_failure_is_retried_but_never_cached(tmp_path, monkeypatch):
    catalog, git = repository(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(cache))
    link = "https://github.com/example/repo/pull/1"
    source = "# Catalog\n| F01 | old |\n[proof](" + link + ")\n"
    invalid = {"edits": [{"old": link, "new": "removed"}]}
    valid = {"edits": [{"old": "| F01 | old |", "new": "| F01 | reviewed |"}]}
    with patch.object(updater, "evidence_for", return_value={"catalog": source}), patch.object(
        updater, "request_edits", side_effect=[invalid, invalid, valid]
    ) as request:
        reviewed, counts = updater.review_batch(tmp_path, "inert", "http://localhost/v1",
                                               "deepseek-v4.1-flash", source, "a" * 40,
                                               "b" * 40, ["feature.py"], cache)
    assert request.call_count == 3
    assert counts == [1]
    assert link in reviewed
    entries = list((cache / "responses").glob("*.json"))
    assert len(entries) == 1
    assert json.loads(entries[0].read_text())["response"] == valid
    catalog.write_text(catalog.read_text() + "[proof](" + link + ")\n", encoding="utf-8")
    git("add", "FORK_FEATURES.md")
    git("commit", "-qm", "catalog provenance")
    before = catalog.read_bytes()
    with patch.object(updater, "request_edits", side_effect=[invalid, invalid, invalid]) as request:
        with pytest.raises(updater.ModelEditMismatch, match="provenance link"):
            updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    assert request.call_count == 3
    assert catalog.read_bytes() == before
