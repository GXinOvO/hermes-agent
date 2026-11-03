"""Existing Git index locks refuse snapshots before traversing the project."""
from pathlib import Path
import subprocess

import pytest

from tools import checkpoint_manager as cm


@pytest.fixture
def project_store(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "data.bin").write_bytes(b"original\x00\xff")
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    manager = cm.CheckpointManager(enabled=True, max_total_size_mb=0)
    assert manager.ensure_checkpoint(str(project), "before")
    manager.new_turn()
    store = cm._store_path()
    index = cm._index_path(store, cm._project_hash(str(project)))
    return manager, project, store, index


@pytest.mark.parametrize("index_present", [True, False])
def test_existing_lock_refuses_before_project_walk(project_store, monkeypatch, caplog, index_present):
    manager, project, _store, index = project_store
    if not index_present:
        index.unlink()
    lock = Path(str(index) + ".lock")
    lock.write_bytes(b"foreign writer evidence")
    identity = lock.stat()
    before_history = manager.list_checkpoints(str(project))
    (project / "data.bin").write_bytes(b"changed")
    scans = []
    original = cm._dir_file_count

    def observed(path):
        scans.append(path)
        return original(path)

    monkeypatch.setattr(cm, "_dir_file_count", observed)
    with caplog.at_level("DEBUG", logger="tools.checkpoint_manager"):
        assert manager.ensure_checkpoint(str(project), "blocked") is False
    assert scans == [], "a known blocked checkpoint must not traverse the project"
    assert "index lock" in caplog.text.lower()
    assert lock.read_bytes() == b"foreign writer evidence"
    assert lock.stat().st_ino == identity.st_ino
    assert manager.list_checkpoints(str(project)) == before_history
    assert (project / "data.bin").read_bytes() == b"changed"


@pytest.mark.parametrize("safe", [False, True], ids=["full", "safe"])
def test_blocked_restore_refuses_before_any_working_file_change(project_store, safe):
    manager, project, _store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    data = project / "data.bin"
    created = project / "created.txt"
    data.write_bytes(b"current data")
    created.write_bytes(b"agent created")
    assert manager.ensure_checkpoint(str(project), "current")
    manager.record_agent_write(str(data))
    manager.record_agent_write(str(created))
    lock = Path(str(index) + ".lock")
    lock.write_bytes(b"foreign writer evidence")
    identity = lock.stat()
    history = manager.list_checkpoints(str(project))
    result = manager.restore(str(project), target, safe=safe)
    assert created.exists(), "blocked safe restore must not delete an agent-created file"
    assert created.read_bytes() == b"agent created"
    assert data.read_bytes() == b"current data"
    assert result["success"] is False, result
    assert "index lock" in result["error"].lower(), result
    assert lock.stat().st_ino == identity.st_ino
    assert lock.read_bytes() == b"foreign writer evidence"
    assert manager.list_checkpoints(str(project)) == history


def test_safe_restore_stops_when_lock_appears_at_staging(project_store, monkeypatch):
    manager, project, _store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    created = project / "created.txt"
    created.write_bytes(b"agent created")
    assert manager.ensure_checkpoint(str(project), "current")
    manager.record_agent_write(str(created))
    original = cm._run_git
    injected = []
    lock = Path(str(index) + ".lock")

    def race(args, *positional, **kwargs):
        if args == ["add", "-A"] and not injected:
            lock.write_bytes(b"new concurrent lock")
            injected.append(True)
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(cm, "_run_git", race)
    result = manager.restore(str(project), target, safe=True)
    assert injected
    assert created.exists(), "a failed staging operation must not start safe restore"
    assert created.read_bytes() == b"agent created"
    assert result["success"] is False, result
    assert lock.read_bytes() == b"new concurrent lock"


def test_safe_restore_keeps_created_file_when_checkout_is_blocked(project_store, monkeypatch):
    manager, project, _store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    data = project / "data.bin"
    data.write_bytes(b"current data")
    created = project / "created.txt"
    created.write_bytes(b"agent created")
    assert manager.ensure_checkpoint(str(project), "current")
    manager.record_agent_write(str(data))
    manager.record_agent_write(str(created))
    original = cm._run_git
    injected = []
    lock = Path(str(index) + ".lock")

    def race(args, *positional, **kwargs):
        if args[0] == "checkout" and not injected:
            lock.write_bytes(b"checkout concurrent lock")
            injected.append(True)
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(cm, "_run_git", race)
    result = manager.restore(str(project), target, safe=True)
    assert injected
    assert created.exists(), "failed checkout must precede agent-created file deletion"
    assert created.read_bytes() == b"agent created"
    assert data.read_bytes() == b"current data"
    assert result["success"] is False, result
    assert lock.read_bytes() == b"checkout concurrent lock"


@pytest.mark.parametrize("lock_phase", ["before_snapshot", "snapshot_stage"])
def test_delete_only_restore_requires_safety_snapshot_and_can_retry(project_store, monkeypatch, lock_phase):
    manager, project, store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    created = project / "created.txt"
    created.write_bytes(b"unique unsaved bytes\x00\xff")
    manager.record_agent_write(str(created))
    before = manager.list_checkpoints(str(project))
    lock = Path(str(index) + ".lock")
    original_take = manager._take
    original_git = cm._run_git
    owned = []
    in_snapshot = []

    def snapshot(*args, **kwargs):
        in_snapshot.append(True)
        if lock_phase == "before_snapshot":
            lock.write_bytes(b"fixture late writer")
            owned.append(lock.stat().st_ino)
        try:
            return original_take(*args, **kwargs)
        finally:
            in_snapshot.clear()

    def stage(args, *rest, **kwargs):
        if in_snapshot and lock_phase == "snapshot_stage" and args == ["add", "-A"]:
            lock.write_bytes(b"fixture late writer")
            owned.append(lock.stat().st_ino)
        return original_git(args, *rest, **kwargs)

    with monkeypatch.context() as control:
        control.setattr(manager, "_take", snapshot)
        control.setattr(cm, "_run_git", stage)
        result = manager.restore(str(project), target, safe=True)
    assert owned
    assert created.exists(), "failed safety snapshot must not delete the only saved copy"
    assert created.read_bytes() == b"unique unsaved bytes\x00\xff"
    assert result["success"] is False, result
    assert manager.list_checkpoints(str(project)) == before
    assert lock.stat().st_ino == owned[0]
    assert lock.read_bytes() == b"fixture late writer"
    lock.unlink()  # The test releases only the exact fixture lock it created.
    result = manager.restore(str(project), target, safe=True)
    assert result["success"], result
    assert not created.exists()
    safety = manager.list_checkpoints(str(project))[0]["hash"]
    saved = subprocess.check_output(
        ["git", "show", f"{safety}:created.txt"],
        env=cm._git_env(store, str(project)), cwd=project,
    )
    assert saved == b"unique unsaved bytes\x00\xff"


@pytest.mark.parametrize("already_saved", [False, True])
def test_delete_only_restore_normal_and_unchanged_safety_snapshot(project_store, already_saved):
    manager, project, store, _index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    created = project / "created.txt"
    created.write_bytes(b"agent created content")
    manager.record_agent_write(str(created))
    if already_saved:
        assert manager.ensure_checkpoint(str(project), "current already saved")
    result = manager.restore(str(project), target, safe=True)
    assert result["success"], result
    assert not created.exists()
    safety = manager.list_checkpoints(str(project))[0]["hash"]
    ok, saved, error = cm._run_git(["show", f"{safety}:created.txt"], store, str(project))
    assert ok, error
    assert saved == "agent created content"


def test_delete_only_restore_refuses_lock_after_safety_snapshot(project_store, monkeypatch):
    manager, project, _store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    created = project / "created.txt"
    created.write_bytes(b"agent created content")
    manager.record_agent_write(str(created))
    original_git = cm._run_git
    lock = Path(str(index) + ".lock")
    owned = []

    def race(args, *rest, **kwargs):
        result = original_git(args, *rest, **kwargs)
        if args == ["cat-file", "-e", f"{target}:created.txt"]:
            lock.write_bytes(b"fixture post-snapshot writer")
            owned.append(lock.stat().st_ino)
        return result

    monkeypatch.setattr(cm, "_run_git", race)
    result = manager.restore(str(project), target, safe=True)
    assert owned
    assert created.exists(), "pure deletion must arbitrate an index lock without a checkout"
    assert created.read_bytes() == b"agent created content"
    assert result["success"] is False, result
    assert lock.stat().st_ino == owned[0]
    assert lock.read_bytes() == b"fixture post-snapshot writer"


def test_delete_phase_holds_git_lock_and_releases_its_own_lock(project_store, monkeypatch):
    manager, project, store, index = project_store
    target = manager.list_checkpoints(str(project))[0]["hash"]
    created = project / "created.txt"
    created.write_bytes(b"agent created content")
    manager.record_agent_write(str(created))
    lock = Path(str(index) + ".lock")
    original_unlink = Path.unlink
    observations = []

    def observed_unlink(path, *args, **kwargs):
        if path == created:
            identity = lock.stat()
            ok, _, _ = cm._run_git(["add", "-A"], store, str(project), index_file=index)
            observations.append((ok, identity.st_ino == lock.stat().st_ino))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", observed_unlink)
    result = manager.restore(str(project), target, safe=True)
    assert result["success"], result
    assert observations == [(False, True)]
    assert not created.exists()
    assert not lock.exists()


def test_delete_lock_released_after_interrupt_before_unlink(project_store, monkeypatch):
    from tools.checkpoint_locks import delete_restore_paths

    _manager, project, _store, index = project_store
    created = project / "created.txt"
    created.write_bytes(b"still present")
    original_unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == created:
            raise KeyboardInterrupt()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(KeyboardInterrupt):
        delete_restore_paths(index, str(project), [created.name])
    assert created.read_bytes() == b"still present"
    assert not Path(str(index) + ".lock").exists()


def test_unobstructed_snapshot_and_restore_still_capture_original(project_store):
    manager, project, _store, _index = project_store
    original = manager.list_checkpoints(str(project))[0]["hash"]
    (project / "data.bin").write_bytes(b"new contents")
    assert manager.ensure_checkpoint(str(project), "second")
    result = manager.restore(str(project), original)
    assert result["success"], result
    assert (project / "data.bin").read_bytes() == b"original\x00\xff"
