"""Files-pane search, behind the existing authenticated filesystem boundary."""
import asyncio
import json
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from hermes_cli.file_search import SearchRequest, search_files, validate_search_root
from hermes_cli.web_routers.files import _fs_backend, _fs_download_path, _fs_owned_session, _fs_profile_home
from hermes_cli.web_server_files import (
    _agent_container_path, _agent_path_scope, _hosted_fs_read_guard, _is_container_cwd,
    _is_unmapped_container_path, _session_workspace,
)

router = APIRouter()


class _SearchResponse(StreamingResponse):
    def __init__(self, stream):
        super().__init__(stream, media_type="application/x-ndjson",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.stream = stream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # StreamingResponse does not close an iterator suspended at yield
            # when sending fails/disconnect wins. It still owns the rg producer.
            with anyio.CancelScope(shield=True):
                await self.stream.aclose()


def _container_search_root(raw: str, profile: str | None, session_id: str | None, session) -> Path | None:
    home = _fs_profile_home(profile)
    with _agent_path_scope(home):
        if not _is_unmapped_container_path(raw, profile_home=home):
            return None
        stored_cwd = (session or {}).get("cwd") or ""
        key, cwd = _session_workspace(session_id, stored_cwd) if session_id else ("", "")
        if session_id and not key and stored_cwd and not _is_container_cwd(stored_cwd):
            # The common mapper drops a deleted host cwd. Its identity still
            # selects this session's sandbox; losing it would select the shared
            # profile workspace, which may belong to a different container.
            from tools.terminal_tool import workspace_key_for

            if not Path(stored_cwd).expanduser().is_absolute():
                raise HTTPException(409, "This session's workspace is unavailable")
            key, cwd = workspace_key_for(str(Path(stored_cwd).expanduser())), stored_cwd
        target = _agent_container_path(raw, profile_home=home, workspace_key=key,
                                       workspace_cwd=cwd, own_sandbox_only=bool(session_id))
        if target is None:
            raise HTTPException(404, "Search root is unavailable in this session's Docker workspace")
        return target


@router.post("/api/fs/search")
async def fs_search(payload: SearchRequest, request: Request, profile: str | None = None,
                    session_id: str | None = None):
    session = await _fs_owned_session(session_id, profile)
    # A Desktop SSH connection reaches this endpoint ON the file host. A terminal
    # profile with yet another SSH hop needs that adapter's own search capability;
    # never search the gateway's local disk under the same pathname instead.
    if await asyncio.to_thread(_fs_backend, profile) is not None:
        raise HTTPException(501, "Search is not supported by this SSH workspace adapter")
    target = await asyncio.to_thread(_container_search_root, payload.root, profile, session_id, session)
    if target is None:
        target = await _fs_download_path(payload.root, profile, session_id, session=session,
                                         decode_fallback=False, own_sandbox_only=bool(session_id))
    _hosted_fs_read_guard(target, request)
    try:
        await asyncio.to_thread(validate_search_root, payload.model_copy(update={"root": str(target)}))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc

    async def frames():
        # ASGI cancellation can land while the outer iterator is suspended at
        # its own yield. Explicitly close the inner engine instead of relying
        # on async-generator GC (which may leave rg alive until its deadline).
        stream = search_files(payload, root_override=target)
        try:
            async for frame in stream:
                yield json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        finally:
            # Starlette's disconnect scope applies level cancellation at every
            # await; shield the join so cleanup can drain and reap subprocesses.
            with anyio.CancelScope(shield=True):
                await stream.aclose()

    return _SearchResponse(frames())
