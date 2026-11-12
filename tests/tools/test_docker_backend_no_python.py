import json
import os

import pytest

from tests.tools.test_docker_backend_real import _run

pytest_plugins = ["tests.tools.test_docker_backend_real"]


pytestmark = pytest.mark.skipif(
    not os.getenv("HERMES_TEST_DOCKER_IMAGE"), reason="HERMES_TEST_DOCKER_IMAGE not set"
)


def test_missing_python_refuses_file_writes_without_mutation(docker_task):
    from tools.file_tools import read_file_tool, write_file_tool

    assert _run("command -p python3 -V", docker_task)["exit_code"] == 127
    path = "/workspace/no-python-existing.txt"
    assert _run(f"printf original > {path}", docker_task)["exit_code"] == 0
    result = json.loads(write_file_tool(path, "replacement", task_id=docker_task))
    assert result.get("error"), result
    assert _run(f"cat {path}", docker_task)["output"] == "original"
    new_path = "/workspace/no-python-new.txt"
    result = json.loads(write_file_tool(new_path, "new", task_id=docker_task))
    assert result.get("error"), result
    assert _run(f"test ! -e {new_path}", docker_task)["exit_code"] == 0
    result = json.loads(read_file_tool(path, task_id=docker_task))
    assert result.get("error"), result
