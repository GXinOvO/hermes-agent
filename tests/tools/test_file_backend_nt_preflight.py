import json
from unittest.mock import patch

import pytest

from tools.file_tools import patch_tool, read_file_tool, search_tool, write_file_tool


BAD = "\\??\\UNC\\attacker.example\\share\\x"


@pytest.mark.parametrize("operation", ["read", "write", "replace", "v4a", "search"])
def test_namespace_rejection_precedes_backend_initialization(operation):
    with patch("tools.file_tools._get_file_ops", side_effect=AssertionError("backend must not initialize")) as backend:
        operations = {
            "read": lambda: read_file_tool(BAD),
            "write": lambda: write_file_tool(BAD, "data"),
            "replace": lambda: patch_tool(mode="replace", path=BAD, old_string="a", new_string="b"),
            "v4a": lambda: patch_tool(mode="patch", patch="*** Begin Patch\n*** Update File: " + BAD +
                                      "\n@@\n-a\n+b\n*** End Patch"),
            "search": lambda: search_tool("x", path=BAD),
        }
        result = operations[operation]()
        assert "NT/device namespace" in json.loads(result)["error"]
        backend.assert_not_called()


def test_ordinary_write_still_enters_its_backend():
    with patch("tools.file_tools._get_file_ops", side_effect=RuntimeError("ordinary backend reached")) as backend:
        result = json.loads(write_file_tool("ordinary.txt", "data"))
    assert "ordinary backend reached" in result["error"]
    backend.assert_called_once()
