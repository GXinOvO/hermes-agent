import http.client
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.scripts.test_update_fork_catalog_snapshots import repository

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/update_fork_catalog.py"
SPEC = importlib.util.spec_from_file_location("catalog_pr_subject", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


@contextmanager
def reply(value):
    class Reply:
        def read(self):
            if isinstance(value, BaseException):
                raise value
            return json.dumps(value).encode()

    yield Reply()


@pytest.mark.parametrize("recover", [True, False])
def test_incomplete_pr_response_retries_once_without_accepting_partial_json(monkeypatch, recover):
    monkeypatch.setenv("GH_TOKEN", "inert-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "GXinOvO/hermes-agent")
    sha = "a" * 40
    row = {"number": 1, "title": "Feature", "body": "proof", "html_url": "https://github.com/example/repo/pull/1",
           "merged_at": "2026-11-15T00:00:00Z", "merge_commit_sha": sha}
    incomplete = http.client.IncompleteRead(b'[{"number":', 100)
    second = [row] if recover else incomplete
    with patch.object(updater.urllib.request, "urlopen", side_effect=[reply(incomplete), reply(second)]) as fetch:
        if recover:
            assert updater.pull_requests(Path("."), {sha}) == [
                {"number": 1, "title": "Feature", "body": "proof", "url": row["html_url"]}
            ]
        else:
            with pytest.raises(RuntimeError, match="GitHub PR response was incomplete"):
                updater.pull_requests(Path("."), {sha})
    assert fetch.call_count == 2
    assert fetch.call_args_list[0].args[0] is fetch.call_args_list[1].args[0]


def test_invalid_complete_pr_json_is_not_retried(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "inert-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "GXinOvO/hermes-agent")

    class Reply:
        def read(self):
            return b'[{"number":'

    @contextmanager
    def invalid():
        yield Reply()

    with patch.object(updater.urllib.request, "urlopen", return_value=invalid()) as fetch:
        with pytest.raises(ValueError):
            updater.pull_requests(Path("."), {"a" * 40})
    fetch.assert_called_once()


def test_incomplete_pr_failure_preserves_catalog_and_checkpoint(tmp_path, monkeypatch):
    catalog, _ = repository(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "inert-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "GXinOvO/hermes-agent")
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    before = catalog.read_bytes()
    incomplete = http.client.IncompleteRead(b"[", 100)
    with patch.object(updater.urllib.request, "urlopen", side_effect=[reply(incomplete), reply(incomplete)]) as fetch:
        with patch.object(updater, "request_edits") as request:
            with pytest.raises(RuntimeError, match="GitHub PR response was incomplete"):
                updater.update(tmp_path, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
            request.assert_not_called()
    assert fetch.call_count == 2
    assert catalog.read_bytes() == before
    assert not (tmp_path / ".FORK_FEATURES.md.pending").exists()
