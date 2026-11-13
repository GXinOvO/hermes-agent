"""A chat's ``cd`` is profile-private state: it must not ride the routing index (#101719).

The routing index is ONE store shared by every profile in a multiplexed gateway, so a
``terminal_cwd`` serialized with the entry leaked one tenant's workspace into another tenant's
``state.db``/``sessions.json``. The cwd now lives in the owning profile's own store, and the
routing entry never carries it.
"""
import json
from datetime import datetime

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def _source(chat_id="c1"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm",
                         user_id="u1", user_name="tester")


def _project(tmp_path, name="project"):
    path = tmp_path / name
    path.mkdir()
    return path


def _routing_entries(store):
    return store._routing_db.load_gateway_routing_entries(scope=store._routing_scope())


def test_a_legacy_index_entry_migrates_the_cwd_out_of_the_shared_index(tmp_path):
    """An index written before this change still carries the cwd: reading it migrates the value into
    the owning profile's store and drops it from the routing entry."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    project = _project(tmp_path)
    entry.metadata["terminal_cwd"] = str(project)  # what an older index loaded

    assert store.get_persisted_terminal_cwd(entry.session_key) == str(project)
    assert "terminal_cwd" not in json.loads(_routing_entries(store)[entry.session_key])["metadata"]
    mirror = (tmp_path / "sessions" / "sessions.json").read_text(encoding="utf-8")
    assert "terminal_cwd" not in mirror and str(project) not in mirror

    store.close_all_db_handles()
    restarted = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert restarted.get_persisted_terminal_cwd(entry.session_key) == str(project)


def test_a_turn_marker_write_keeps_the_cwd_out_of_the_routing_entry(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    project = _project(tmp_path)
    assert store.set_session_metadata(entry.session_key, "terminal_cwd", str(project))

    assert store.mark_turn_active(entry.session_key)

    stored = json.loads(_routing_entries(store)[entry.session_key])
    assert "terminal_cwd" not in stored["metadata"] and stored["active_turn_token"]


def test_the_saved_cwd_survives_a_restart(tmp_path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    project = _project(tmp_path)
    assert store.set_session_metadata(entry.session_key, "terminal_cwd", str(project))
    store.close_all_db_handles()

    restarted = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    assert restarted.get_persisted_terminal_cwd(entry.session_key) == str(project)
    assert "terminal_cwd" not in restarted.get_or_create_session(_source()).metadata


def test_without_sqlite_the_cwd_stays_in_a_profile_private_sidecar(tmp_path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    store._db = None
    entry = store.get_or_create_session(_source())
    project = _project(tmp_path)

    assert store.set_session_metadata(entry.session_key, "terminal_cwd", str(project))
    sidecar = sessions_dir / ".terminal_cwd.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {entry.session_id: str(project)}
    assert str(project) not in (sessions_dir / "sessions.json").read_text(encoding="utf-8")

    assert store.clear_persisted_terminal_cwd(entry.session_key)
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {}
    assert store.get_persisted_terminal_cwd(entry.session_key) is None


def test_a_named_profiles_cwd_never_reaches_the_shared_root_store(tmp_path, monkeypatch):
    from hermes_cli import profiles

    homes = {"alpha": tmp_path / "profiles" / "alpha", "beta": tmp_path / "profiles" / "beta"}
    for home in homes.values():
        (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in homes)
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: str(homes[name]))
    root = tmp_path / "root"
    (root / "sessions").mkdir(parents=True)

    store = SessionStore(sessions_dir=root / "sessions", config=GatewayConfig(multiplex_profiles=True))
    store._routing_home = root
    keys = {name: f"agent:{name}:telegram:dm:1" for name in homes}
    canaries = {name: str(_project(tmp_path, f"{name}-workspace")) for name in homes}
    store._ensure_loaded()
    with store._lock:
        for name, key in keys.items():
            now = datetime.now()
            store._entries[key] = SessionEntry(session_key=key, session_id=f"sid-{name}",
                                               created_at=now, updated_at=now)

    for name, key in keys.items():
        assert store.set_session_metadata(key, "terminal_cwd", canaries[name])
        assert store.get_session_metadata(key, "terminal_cwd") == canaries[name]

    for name, key in keys.items():
        other = [canary for owner, canary in canaries.items() if owner != name]
        stored = json.loads(_routing_entries(store)[key])
        assert "terminal_cwd" not in stored["metadata"]
        for root_file in list(root.glob("state.db*")) + [root / "sessions" / "sessions.json"]:
            if root_file.is_file():
                leaked = root_file.read_bytes()
                assert canaries[name].encode() not in leaked, root_file
                assert not any(canary.encode() in leaked for canary in other), root_file
    store.close_all_db_handles()
