"""The dashboard session token: where its value comes from.

The desktop shell mints the token via HERMES_DASHBOARD_SESSION_TOKEN; a
"file:<path>" value instead names a file to read it from (the Nix modules
render backend.sessionTokenFile this way, so a path -- not a secret -- sits
in the service environment). With no variable, a fresh token is minted per
server start. It dies with the process and is injected into the SPA HTML so
only the web UI can use it.
"""

import os
import secrets
import json
import stat
from contextlib import contextmanager
from pathlib import Path


def resolve_session_token() -> str:
    env_token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN")
    if not env_token:
        return secrets.token_urlsafe(32)
    # A "file:" prefix means the value is a token-file path, not a token:
    # deployment managers (agenix, sops-nix, systemd credentials) hand a
    # secret over as a file, and a path -- unlike the token -- can sit in a
    # systemd Environment= or a launchd plist without putting anything
    # readable into a store path.
    if env_token.startswith("file:"):
        token_file = env_token[len("file:") :]
        try:
            token = Path(token_file).read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as exc:
            raise SystemExit(
                f"HERMES_DASHBOARD_SESSION_TOKEN names an unreadable token file ({token_file}): {exc}"
            ) from exc
        if not token:
            raise SystemExit(
                f"HERMES_DASHBOARD_SESSION_TOKEN names an empty token file ({token_file})"
            )
        return token
    return env_token


@contextmanager
def ssh_input_bootstrap(directory, nonce):
    if directory is None or not nonce or os.name == 'nt':
        yield None
        return
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    proof = {'key': secrets.token_urlsafe(32), 'instance_id': secrets.token_hex(16), 'tickets': {}}
    name = f'{nonce}.{proof["instance_id"]}.input-key'
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != getattr(os, 'getuid', lambda: -1)() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('SSH input directory must be private')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        created = os.fstat(fd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'key': proof['key'], 'instance_id': proof['instance_id'], 'pid': os.getpid()}, stream)
            yield proof
        finally:
            for store in proof['tickets'].values():
                store.revoke()
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)


def bind_ssh_input_connection(ws):
    from fastapi import HTTPException
    from hermes_cli.web_server_profiles import _resolve_profile_dir
    from hermes_constants import get_process_hermes_home

    ticket = ws.headers.get('x-hermes-ssh-input-ticket')
    if ticket is None:
        return 'ssh_input_missing' if ws.query_params.get('ssh_dial') else None
    proof = get_ssh_input_proof()
    if proof is None or ws.url.path != '/api/ws':
        return 'ssh_input_invalid'
    profile = ws.headers.get('x-hermes-ssh-input-profile', 'current')
    try:
        home = str((get_process_hermes_home() if profile in ('', 'current')
                    else _resolve_profile_dir(profile)).resolve())
        store = proof['tickets'].get(home)
        if store is None:
            return 'ssh_input_invalid'
        store.redeem(ticket, profile_id=home, purpose='ssh-input')
    except (PermissionError, HTTPException):
        return 'ssh_input_invalid'
    ws.scope['hermes.ssh_input'] = {'instance_id': proof['instance_id'], 'profile_home': home,
                                    'connection_id': secrets.token_hex(16)}
    return None


def get_ssh_input_proof():
    from hermes_cli.web_server import app, _ssh_runtime_intact

    proof = getattr(app.state, 'ssh_input', None)
    if proof is not None and not _ssh_runtime_intact():
        for store in proof['tickets'].values():
            store.revoke()
        app.state.ssh_input = None
        return None
    return proof
