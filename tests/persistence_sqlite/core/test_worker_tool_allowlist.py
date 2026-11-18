import json
from unittest.mock import MagicMock

import pytest

from hermes_state_runtime import RuntimeStoreError
from hermes_state import SessionDB
from hermes_state_worker_context import worker_tool_names


def test_worker_accepts_explicit_empty_allowlist_pin(tmp_path):
    pin = {"version": "unit-code", "tools": [], "allowed_tools": []}
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("unit-child", "subagent")
        result = db._execute_write(lambda conn: worker_tool_names(db, conn, "unit-child", {"tool_names": pin}))
        assert result == {"value": None}
        assert json.loads(db.get_session("unit-child")["tool_names"])["allowed_tools"] == []


@pytest.mark.parametrize("allowed", [None, "read_file", [7], ["read_file", None]])
def test_worker_rejects_malformed_saved_allowlist(allowed):
    db = MagicMock()
    conn = MagicMock()
    pin = {"version": "unit-code", "tools": [], "allowed_tools": allowed}
    with pytest.raises(RuntimeStoreError):
        worker_tool_names(db, conn, "unit-child", {"tool_names": pin})
    conn.execute.assert_not_called()
