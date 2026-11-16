import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/update_fork_catalog.py"
SPEC = importlib.util.spec_from_file_location("catalog_escape_subject", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def response(content):
    return {"model": "deepseek-v4.1-flash", "choices": [
        {"finish_reason": "stop", "message": {"content": content}}
    ]}


@pytest.mark.parametrize("value", [r"C:\Users\GXinOvO", r"literal \q and \."])
def test_invalid_escape_is_repaired_without_changing_literal_text(value):
    raw = '{"edits":[{"old":"| F01 | old |","new":"| F01 | ' + value + ' |"}]}'
    parsed = updater.parse_response(response(raw), "deepseek-v4.1-flash")
    assert parsed == {"edits": [{"old": "| F01 | old |", "new": "| F01 | " + value + " |"}]}
    assert updater.apply_edits("# Catalog\n| F01 | old |\n", parsed) == "# Catalog\n| F01 | " + value + " |\n"


@pytest.mark.parametrize("value", ['line\nnext', 'tab\tquote"slash\\', "中文 / text"])
def test_valid_json_escapes_remain_unchanged(value):
    expected = {"edits": [{"old": "old", "new": value}]}
    assert updater.parse_response(response(json.dumps(expected)), "deepseek-v4.1-flash") == expected


@pytest.mark.parametrize("raw", [
    '{"edits":[{"old":"old","new":"\\u12G4"}]}',
    '{"edits":[{"old":"old","new":"\\q and \\u12G4"}]}',
    '{"edits":[],}',
    'prefix {"edits":[]}',
])
def test_other_invalid_json_stays_rejected(raw):
    with pytest.raises(ValueError):
        updater.parse_response(response(raw), "deepseek-v4.1-flash")


@pytest.mark.parametrize("change", ["model", "finish", "choices"])
def test_repair_does_not_bypass_model_or_completion_identity(change):
    raw = response('{"edits":[{"old":"old","new":"\\q"}]}')
    if change == "model":
        raw["model"] = "other"
    elif change == "finish":
        raw["choices"][0]["finish_reason"] = "length"
    else:
        raw["choices"].append(raw["choices"][0])
    with pytest.raises(ValueError):
        updater.parse_response(raw, "deepseek-v4.1-flash")
