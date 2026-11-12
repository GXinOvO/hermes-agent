from __future__ import annotations

from pathlib import Path

import pytest

from agent.secret_scope import current_secret_scope, current_secret_scope_home, get_secret, set_multiplex_active
from agent.runtime_scope import bind_profile_scope, bind_state
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_scope import LOCAL_OWNER, LOCAL_TENANT, Principal, Scope, ScopeSwitchRefused, current_scope
from tools.terminal_scope import get_terminal_scope
from tui_gateway import launch_profile_policy as policy


@pytest.fixture(params=[False, True], ids=["single", "multiplex"])
def homes(request, tmp_path, monkeypatch):
    launch = tmp_path / ".hermes"
    work = launch / "profiles" / "work"
    work.mkdir(parents=True)
    (launch / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    (launch / ".env").write_text("LAUNCH_FILE_KEY=launch-file\nSHARED_KEY=launch\n", encoding="utf-8")
    (work / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")
    (work / ".env").write_text("WORK_KEY=work\nSHARED_KEY=work\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("LAUNCH_ENV_ONLY", "env-only")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(policy, "_snapshot", None)
    if request.param:
        policy.activate_multi_profile_hosting()
    try:
        yield launch, work, request.param
    finally:
        set_multiplex_active(False)


def _secrets() -> dict:
    return {k: get_secret(k) for k in ("LAUNCH_FILE_KEY", "LAUNCH_ENV_ONLY", "WORK_KEY", "SHARED_KEY")}


def test_a_secondary_home_gets_only_its_own_files(homes):
    _, work, _ = homes
    with policy.profile_config_scope(work):
        assert get_hermes_home() == work
        assert current_secret_scope_home() == str(work)
        assert _secrets() == {"LAUNCH_FILE_KEY": None, "LAUNCH_ENV_ONLY": None, "WORK_KEY": "work", "SHARED_KEY": "work"}
        assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "docker"
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_the_launch_home_inside_a_secondary_scope_is_the_launch_home(homes):
    launch, work, _ = homes
    with policy.profile_config_scope(work), policy.profile_config_scope(launch):
        assert get_hermes_home() == launch
        assert current_secret_scope_home() is None
        assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                              "SHARED_KEY": "launch"}
        assert (get_terminal_scope() or {}).get("TERMINAL_ENV") != "docker"
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_the_launch_home_keeps_the_launch_rules(homes):
    launch, _, multiplex = homes
    with policy.profile_config_scope(launch):
        assert current_secret_scope() is not None
        assert current_secret_scope_home() is None
        assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                              "SHARED_KEY": "launch"}
        if multiplex:
            assert get_hermes_home_override() == str(launch)
            assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "local"
        else:
            assert (get_hermes_home_override(), get_terminal_scope()) == (None, None)
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_a_bound_scope_is_never_left_half_switched(homes):
    """The config body inherits a bound scope (a routed turn's): it must see ONE profile's home,
    secrets and terminal policy, and come back to the enclosing scope whole."""
    launch, work, _ = homes
    with bind_profile_scope(work):
        assert get_hermes_home() == work
        with policy.profile_config_scope(launch):
            assert get_hermes_home() == launch
            assert current_secret_scope_home() is None
            assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                                  "SHARED_KEY": "launch"}
            assert (get_terminal_scope() or {}).get("TERMINAL_ENV") != "docker"
            assert current_scope() == Scope(LOCAL_TENANT, str(launch), LOCAL_OWNER)
        assert get_hermes_home() == work
        assert current_secret_scope_home() == str(work)
        assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "docker"


def test_a_hosted_scope_refuses_a_config_scope(homes):
    """A hosted activity's agent is fixed by its signed frame: no profile config body may move it,
    and nothing of the requested profile may be bound before the refusal."""
    launch, work, _ = homes
    hosted = Scope("acme", "agent-7", Principal("u1"), owner=Principal("u1"), can_administer=True)
    with bind_state(hosted):
        for home in (work, launch):
            with pytest.raises(ScopeSwitchRefused):
                with policy.profile_config_scope(home):
                    pass
        assert current_scope() == hosted
        assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_a_failed_bind_leaves_nothing_bound(homes, monkeypatch):
    """The three pieces are computed before any is installed: a secondary whose files cannot be
    read leaves the caller's state alone instead of a home pointing at a profile with no secrets."""
    _, work, _ = homes
    from agent import secret_scope

    def unreadable(_home):
        raise RuntimeError("profile files unreadable")

    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", unreadable)
    with pytest.raises(RuntimeError):
        with policy.profile_config_scope(work):
            pass
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)
