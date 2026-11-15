from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.file_operations import ShellFileOperations, _TooLarge
from tools.file_protected_fuse import ReadTicket, Request
from tools.file_protected_patch import ProtectedPatch, _active_patch


@pytest.mark.parametrize("entrypoint", ["exact", "raw"])
@pytest.mark.parametrize("content,limit,refused", [
    (b"abcd", 4, False), (b"abcde", 4, True), (b"abcde", None, False),
])
def test_protected_patch_snapshot_obeys_read_cap(content, limit, refused, entrypoint, monkeypatch):
    env = SimpleNamespace(cwd="/cas", execute=Mock(side_effect=AssertionError("shell fallback")))
    ops = ShellFileOperations(env)
    packet = Request(abi=2)
    packet.expected.inode = 1
    ticket = ReadTicket("/cas/file", "generation", "task", bytes(packet), "[]", (),
                        "storage", sha256(content).hexdigest(), 1, content)
    state = ProtectedPatch(env, "task", tickets={ticket.path: ticket})
    token = _active_patch.set(state)
    try:
        if entrypoint == "raw":
            monkeypatch.setattr("tools.file_operations._RAW_READ_MAX_BYTES", limit or len(content))
            result = ops.read_file_raw(ticket.path)
            data, failure = (None, result) if result.error else (result.content.encode(), None)
        else:
            data, failure = ops._read_exact_bytes(ticket.path, max_bytes=limit)
        if refused:
            assert data is None
            if entrypoint == "raw":
                assert failure._too_large and "too large" in failure.error
            else:
                assert isinstance(failure, _TooLarge) and "too large" in failure.stdout
        else:
            assert (data, failure) == (content, None)
        assert state.target(ticket.path) is ticket
        env.execute.assert_not_called()
    finally:
        _active_patch.reset(token)
