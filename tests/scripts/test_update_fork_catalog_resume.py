import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/update_fork_catalog.py"
SPEC = importlib.util.spec_from_file_location("catalog_resume_subject", SCRIPT)
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
    catalog.write_text("# Catalog\n| F01 | old |\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    catalog.write_text(updater.set_checkpoint(catalog.read_text(), base))
    for name in ["first.txt", "second.txt"]:
        (root / name).write_text(name + "x" * 60_000)
    git("add", ".")
    git("commit", "-qm", "features")
    return catalog, git


def test_completed_batches_resume_in_fresh_process_after_interruption(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    catalog, git = repository(root)
    before = catalog.read_bytes()
    target = git("rev-parse", "HEAD")
    cache = tmp_path / "cache"
    log = tmp_path / "calls.jsonl"
    worker = tmp_path / "worker.py"
    worker.write_text('''import importlib.util,json,sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location("subject",sys.argv[1])
subject=importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
root,log,mode=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
def request(key,base_url,model,evidence):
    paths=evidence["changed_paths"]
    with log.open("a") as stream:stream.write(json.dumps({"mode":mode,"paths":paths,"target":evidence["target"]})+"\\n")
    if mode=="first" and paths=="second.txt":raise RuntimeError("interrupted before second batch")
    if mode=="resume":
        if paths!="second.txt":raise RuntimeError("completed batch was repeated or frozen target changed")
        old,new="| F01 | first |","| F01 | final |"
    else:old,new="| F01 | old |","| F01 | first |"
    return {"edits":[{"old":old,"new":new}]}
try:
    with patch.object(subject,"request_edits",side_effect=request):
        subject.update(root,"inert-test-key","http://localhost/v1","deepseek-v4.1-flash")
except RuntimeError as exc:
    print(str(exc),file=sys.stderr)
    raise SystemExit(21)
''')
    env = dict(os.environ, FORK_CATALOG_CACHE_DIR=str(cache), PYTHONDONTWRITEBYTECODE="1")
    def invoke(mode):
        return subprocess.run([sys.executable, "-B", str(worker), str(SCRIPT), str(root), str(log), mode],
                              env=env, capture_output=True, text=True, timeout=30, check=False)
    first = invoke("first")
    assert first.returncode == 21, first.stderr
    assert "interrupted before second batch" in first.stderr
    assert catalog.read_bytes() == before
    assert updater.checkpoint(catalog.read_text()) != target
    (root / "third.txt").write_text("newer change must remain pending\n")
    git("add", ".")
    git("commit", "-qm", "new push while review pending")
    newer_head = git("rev-parse", "HEAD")
    second = invoke("resume")
    assert second.returncode == 0, second.stderr + second.stdout
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["paths"] for row in rows] == ["first.txt", "second.txt", "second.txt"]
    assert rows[-1]["target"] == target
    assert "| F01 | final |" in catalog.read_text()
    assert updater.checkpoint(catalog.read_text()) == target
    assert updater.checkpoint(catalog.read_text()) != newer_head
    assert "inert-test-key" not in "".join(p.read_text() for p in cache.rglob("*.json"))


def test_split_decision_survives_retry_without_repeating_incomplete_parent(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    catalog, _ = repository(root)
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(updater, "MAX_DIFF_CHARS", 200_000)
    before = catalog.read_bytes()
    first = {"edits": [{"old": "| F01 | old |", "new": "| F01 | first |"}]}
    final = {"edits": [{"old": "| F01 | first |", "new": "| F01 | final |"}]}
    with patch.object(updater, "request_edits", side_effect=[
        updater.IncompleteModelResponse("incomplete parent"), first, RuntimeError("offline child")
    ]) as initial:
        try:
            updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        except RuntimeError as error:
            assert str(error) == "offline child"
        else:
            raise AssertionError("expected second child failure")
        assert initial.call_count == 3
    assert catalog.read_bytes() == before
    with patch.object(updater, "request_edits", return_value=final) as resumed:
        assert updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        resumed.assert_called_once()
        assert resumed.call_args.args[-1]["changed_paths"] == "second.txt"
    assert "| F01 | final |" in catalog.read_text()


def test_source_samples_keep_frozen_target_after_checkout_advances(tmp_path):
    catalog, git = repository(tmp_path)
    (tmp_path / "small.py").write_text("value = 'first'\n")
    git("add", ".")
    git("commit", "-qm", "small source")
    target = git("rev-parse", "HEAD")
    (tmp_path / "small.py").write_text("value = 'newer'\n")
    git("add", ".")
    git("commit", "-qm", "newer source")
    evidence = updater.evidence_for(tmp_path, catalog.read_text(), updater.checkpoint(catalog.read_text()),
                                    target, ["small.py"])
    assert evidence["source_samples"] == [{"path": "small.py", "source": "value = 'first'\n"}]
    assert evidence["target"] == target


def test_cached_edits_are_validated_before_reuse(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    catalog, _ = repository(root)
    cache = tmp_path / "cache"
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(cache))
    before = catalog.read_bytes()
    first = {"edits": [{"old": "| F01 | old |", "new": "| F01 | first |"}]}
    with patch.object(updater, "request_edits", side_effect=[first, RuntimeError("offline")]):
        try:
            updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        except RuntimeError:
            pass
    entries = list((cache / "responses").glob("*.json"))
    assert len(entries) == 1
    saved = json.loads(entries[0].read_text())
    saved["response"]["edits"][0]["new"] = "gone"
    entries[0].write_text(json.dumps(saved))
    final = {"edits": [{"old": "| F01 | first |", "new": "| F01 | final |"}]}
    with patch.object(updater, "request_edits", side_effect=[first, final]) as fresh:
        assert updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        assert fresh.call_count == 2
    assert "| F01 | final |" in catalog.read_text()
    assert before != catalog.read_bytes()


def test_no_change_observation_does_not_pin_away_future_work(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    catalog, git = repository(root)
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    original = catalog.read_text()
    with patch.object(updater, "request_edits", return_value={"edits": []}):
        assert updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
    git("add", ".")
    git("commit", "-qm", "catalog checkpoint")
    with patch.object(updater, "request_edits") as empty:
        assert not updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        empty.assert_not_called()
    (root / "later.py").write_text("later = True\n")
    git("add", ".")
    git("commit", "-qm", "new feature after idle observation")
    with patch.object(updater, "request_edits", return_value={"edits": []}) as fresh:
        assert updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        fresh.assert_called_once()
    assert updater.checkpoint(catalog.read_text()) == git("rev-parse", "HEAD")
    assert "| F01 | old |" in original


def test_cache_revalidates_inputs_and_preserves_failed_batch_atomicity(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    catalog, git = repository(root)
    cache = tmp_path / "cache"
    monkeypatch.setenv("FORK_CATALOG_CACHE_DIR", str(cache))
    before = catalog.read_bytes()
    first = {"edits": [{"old": "| F01 | old |", "new": "| F01 | first |"}]}
    final = {"edits": [{"old": "| F01 | first |", "new": "| F01 | final |"}]}
    with patch.object(updater, "request_edits", side_effect=[first, RuntimeError("offline")]):
        try:
            updater.update(root, "inert", "http://localhost/v1", "deepseek-v4.1-flash")
        except RuntimeError:
            pass
        else:
            raise AssertionError("later batch failure must propagate")
    assert catalog.read_bytes() == before
    with patch.object(updater, "request_edits", side_effect=[first, final]) as call:
        updater.update(root, "inert", "http://localhost/different", "deepseek-v4.1-flash")
    assert call.call_count == 2
    assert "| F01 | final |" in catalog.read_text()
    assert updater.checkpoint(catalog.read_text()) == git("rev-parse", "HEAD")
