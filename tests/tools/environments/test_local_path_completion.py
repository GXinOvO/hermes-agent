"""Terminal-subshell PATH completion in ``tools/environments/local.py``.

A backend started by a non-interactive SSH session, systemd or a GUI launcher
inherits a PATH without ``~/.local/bin`` (only the login shell adds it), so CLIs
installed there were ``command not found`` from the terminal tool (#111778).
"""

import errno
import os
import shlex
from pathlib import Path

import pytest

from tests.tools import _child_env_fixtures
from tools.environments import local as local_mod
from tools.environments.local import _append_missing_sane_path_entries, _make_run_env

pytestmark = pytest.mark.platforms("posix")  # POSIX PATH completion only
child_env = _child_env_fixtures.child_env


def test_existing_user_local_bin_appended_after_inherited_entries(monkeypatch, tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", list)
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
    monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)

    entries = _make_run_env({})["PATH"].split(os.pathsep)

    assert entries[:2] == ["/usr/bin", "/bin"]
    assert entries.count(str(local_bin)) == 1
    # Already on PATH: position kept, no duplicate appended.
    already = _append_missing_sane_path_entries(f"{local_bin}:/usr/bin").split(":")
    assert already[0] == str(local_bin) and already.count(str(local_bin)) == 1


def test_missing_user_local_bin_not_appended(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)

    assert ".local" not in _append_missing_sane_path_entries("/usr/bin:/bin")


class TestUserLocalBinAliases:
    @pytest.mark.parametrize("path_source", ["inherited", "backend"])
    @pytest.mark.parametrize("existing_spelling", ["resolved", "home", "other-alias"])
    def test_existing_entity_keeps_its_path_spelling(
        self, child_env, monkeypatch, path_source, existing_spelling,
    ):
        physical_local = child_env / "physical-local"
        local_bin = physical_local / "bin"
        local_bin.mkdir(parents=True)
        (child_env / ".local").symlink_to(physical_local, target_is_directory=True)
        other_alias = child_env / "bin-alias"
        other_alias.symlink_to(local_bin, target_is_directory=True)
        existing_bin = {
            "resolved": local_bin,
            "home": child_env / ".local" / "bin",
            "other-alias": other_alias,
        }[existing_spelling]
        project_bin = child_env / "project" / "bin"
        project_bin.mkdir(parents=True)
        shared_sdk = child_env / "shared-sdk" / "bin"
        shared_sdk.mkdir(parents=True)
        shared_alias = child_env / "sdk-alias"
        shared_alias.symlink_to(shared_sdk, target_is_directory=True)
        inherited = [str(project_bin), str(existing_bin), str(shared_alias), str(shared_sdk),
                     "relative-bin", "/usr/bin", "/bin"]
        inherited_path = os.pathsep.join(inherited)
        backend = {}
        monkeypatch.setenv("PATH", inherited_path if path_source == "inherited" else "/usr/bin:/bin")
        if path_source == "backend":
            backend["PATH"] = inherited_path
        monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", list)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
        monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)

        run_env = _make_run_env(backend)
        entries = run_env["PATH"].split(os.pathsep)

        assert entries[:len(inherited)] == inherited
        assert [entry for entry in entries if Path(entry).resolve() == local_bin] == [str(existing_bin)]
        assert run_env["HOME"] == str(child_env)
        assert _append_missing_sane_path_entries(run_env["PATH"]) == run_env["PATH"]

    def test_existing_physical_user_bin_skips_unrelated_path_resolution(self, child_env, monkeypatch):
        local_bin = child_env / ".local" / "bin"
        local_bin.mkdir(parents=True)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
        original_realpath = os.path.realpath
        unrelated = "/unrelated-shared-sdk/bin"

        def resolve(path, *args, **kwargs):
            assert str(path) != unrelated
            return original_realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", resolve)
        inherited = [unrelated, "/usr/bin", str(local_bin), "/bin"]

        entries = _append_missing_sane_path_entries(os.pathsep.join(inherited)).split(os.pathsep)

        assert entries[:len(inherited)] == inherited
        assert entries.count(str(local_bin)) == 1

    def test_missing_symlinked_user_bin_uses_physical_directory(self, child_env, monkeypatch):
        local_bin = child_env / "physical-local" / "bin"
        local_bin.mkdir(parents=True)
        (child_env / ".local").symlink_to(local_bin.parent, target_is_directory=True)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)

        entries = _append_missing_sane_path_entries("/usr/bin:/bin").split(os.pathsep)

        assert entries[:2] == ["/usr/bin", "/bin"]
        assert entries.count(str(local_bin)) == 1
        assert str(child_env / ".local" / "bin") not in entries

    def test_login_snapshot_retains_one_user_bin_and_project_precedence(self, child_env, monkeypatch):
        local_bin = child_env / "physical-local" / "bin"
        local_bin.mkdir(parents=True)
        (child_env / ".local").symlink_to(local_bin.parent, target_is_directory=True)
        project_bin = child_env / "project" / "bin"
        project_bin.mkdir(parents=True)
        shared_sdk = child_env / "shared-sdk" / "bin"
        shared_sdk.mkdir(parents=True)
        for directory, value in [(project_bin, "project"), (local_bin, "user")]:
            tool = directory / "path-completion-probe"
            tool.write_text(f"#!/bin/sh\nprintf '%s\\n' {value}\n", encoding="utf-8")
            tool.chmod(0o755)
        expected_prefix = [str(project_bin), str(local_bin), str(shared_sdk), "/usr/bin", "/bin"]
        shell_init = child_env / "controlled-init.sh"
        shell_init.write_text(f"export HOME={shlex.quote(str(child_env))}\n"
                              f"export PATH={shlex.quote(os.pathsep.join(expected_prefix))}\n", encoding="utf-8")
        monkeypatch.setattr(local_mod, "_resolve_shell_init_files", lambda: [str(shell_init)])
        monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", list)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
        monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)
        monkeypatch.setenv("PATH", os.pathsep.join(expected_prefix))
        backend = local_mod.LocalEnvironment(cwd=str(child_env), timeout=30)
        try:
            result = backend.execute("path-completion-probe; printf '%s\\n' \"$HOME\" \"$PATH\"")
        finally:
            backend.cleanup()

        assert backend._snapshot_ready
        assert result["returncode"] == 0, result
        selected, home, path = result["output"].strip().splitlines()
        assert selected == "project"
        assert home == str(child_env)
        entries = path.split(os.pathsep)
        assert entries[:len(expected_prefix)] == expected_prefix
        assert [entry for entry in entries if Path(entry).resolve() == local_bin] == [str(local_bin)]

    @pytest.mark.parametrize("state, expected_errno", [
        ("missing", errno.ENOENT), ("file", errno.ENOTDIR), ("denied", errno.EACCES),
    ])
    def test_invalid_path_component_does_not_hide_user_tool(
        self, child_env, monkeypatch, state, expected_errno,
    ):
        local_bin = child_env / ".local" / "bin"
        local_bin.mkdir(parents=True)
        tool = local_bin / "path-completion-probe"
        tool.write_text("#!/bin/sh\nprintf '%s\\n' user-bin\n", encoding="utf-8")
        tool.chmod(0o755)
        component = local_bin.parent / state
        if state == "file":
            component.touch()
        elif state == "denied":
            component.mkdir()
            component.chmod(0)
        invalid = str(component) + "/../bin"
        inherited = [invalid, "/usr/bin", "/bin"]
        shell_init = child_env / "controlled-init.sh"
        shell_init.write_text(f"export HOME={shlex.quote(str(child_env))}\n"
                              f"export PATH={shlex.quote(os.pathsep.join(inherited))}\n", encoding="utf-8")
        monkeypatch.setattr(local_mod, "_resolve_shell_init_files", lambda: [str(shell_init)])
        monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", list)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
        monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)
        monkeypatch.setenv("PATH", os.pathsep.join(inherited))
        backend = None
        try:
            if state == "denied" and os.access(component, os.X_OK):
                pytest.skip("host can traverse a directory with search permission denied")
            with pytest.raises(OSError) as failure:
                os.stat(invalid)
            assert failure.value.errno == expected_errno
            backend = local_mod.LocalEnvironment(cwd=str(child_env), timeout=30)
            result = backend.execute("path-completion-probe")

            assert backend._snapshot_ready
            assert result["returncode"] == 0, result
            assert result["output"].strip() == "user-bin"
            entries = _make_run_env({})["PATH"].split(os.pathsep)
            assert entries[:len(inherited)] == inherited
            assert entries.count(str(local_bin)) == 1
        finally:
            if backend is not None:
                backend.cleanup()
            if state == "denied":
                component.chmod(0o700)

    @pytest.mark.parametrize("state", ["missing", "file", "dangling-link"])
    def test_unusable_user_bin_is_not_appended(self, child_env, monkeypatch, state):
        local_bin = child_env / ".local" / "bin"
        local_bin.parent.mkdir()
        if state == "file":
            local_bin.touch()
        elif state == "dangling-link":
            local_bin.symlink_to(child_env / "absent", target_is_directory=True)
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)

        entries = _append_missing_sane_path_entries("/usr/bin:/bin").split(os.pathsep)

        assert not any(entry.startswith(str(child_env)) for entry in entries)

    def test_user_bin_tracks_home_changes_independently_of_profile(self, child_env, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(child_env / "profiles" / "other"))
        monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", list)
        for name in ["first-home", "second-home"]:
            home = child_env / name
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            monkeypatch.setattr(Path, "home", lambda: home)
            monkeypatch.setenv("HOME", str(home))

            entries = _append_missing_sane_path_entries("/usr/bin:/bin").split(os.pathsep)

            assert [entry for entry in entries if entry.startswith(str(child_env))] == [str(local_bin)]
