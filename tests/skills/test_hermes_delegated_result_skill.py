"""Structured child output must pass native, shape and parent material checks."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.delegation_output_schema import validate_output

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "autonomous-ai-agents" / "hermes-agent"
SCRIPT = SKILL / "scripts" / "check-delegated-result.py"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("delegated_material_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def material(tmp_path):
    artifact = tmp_path / "review.md"
    artifact.write_text("Reviewed material.\n", encoding="utf-8")
    receipt = tmp_path / "exit.json"
    receipt.write_text('{"exit_code": 0}', encoding="utf-8")
    return {
        "verdict": "pass", "summary": "Checked material", "artifacts": [artifact.name],
        "findings": [], "checks": [{"command": "fixture", "exit_code": 0, "receipt": receipt.name}],
        "remaining": [],
    }


def envelope(body):
    schema = json.loads((SKILL / "templates" / "delegation-output.schema.json").read_text(encoding="utf-8"))
    raw = json.dumps(body)
    valid, _errors = validate_output(raw, schema)
    return {"status": "completed", "schema_valid": valid, "summary": raw}


def _cli(args):
    code = ("import runpy,sys\n"
            f"sys.argv = {['check', *args]!r}\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=60, check=False)


def test_native_result_to_public_cli(tmp_path, material):
    result = tmp_path / "result.json"
    result.write_text(json.dumps(envelope(material)), encoding="utf-8")
    args = ["--result", str(result), "--required-artifact", "review.md", "--require-checks"]
    good = _cli(args)
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)["accepted"] is True
    (tmp_path / "review.md").unlink()
    bad = _cli(args)
    assert bad.returncode == 1, bad.stderr
    assert any(e.startswith("required_artifact_missing:") for e in json.loads(bad.stdout)["errors"])


def _rewrite(body, result):
    """Re-serialize a mutated body and re-state its native verdict as passing."""
    result["summary"] = json.dumps(body)
    result["schema_valid"] = True


def _set_verdict(body, result, _tmp):
    body["verdict"] = "partial"
    _rewrite(body, result)


def _add_remaining(body, result, _tmp):
    body["remaining"] = ["Not verified"]
    _rewrite(body, result)


def _drop_summary(body, result, _tmp):
    del body["summary"]
    _rewrite(body, result)


def _bool_exit(body, result, _tmp):
    body["checks"][0]["exit_code"] = False
    _rewrite(body, result)


def _failing_exit(body, result, _tmp):
    body["checks"][0]["exit_code"] = 1
    _rewrite(body, result)


def _drop_receipt(body, result, tmp):
    (tmp / "exit.json").unlink()


def _empty_receipt(body, result, tmp):
    (tmp / "exit.json").write_text("", encoding="utf-8")


def _no_checks(body, result, _tmp):
    body["checks"] = []
    _rewrite(body, result)


def _undeclare(body, result, _tmp):
    body["artifacts"] = []
    _rewrite(body, result)


def _empty_artifact(body, result, tmp):
    (tmp / "review.md").write_text("", encoding="utf-8")


def _schema_false(body, result, _tmp):
    result["schema_valid"] = False


def _schema_absent(body, result, _tmp):
    del result["schema_valid"]


def _lifecycle(body, result, _tmp):
    result["status"] = "failed"


def _bad_json(body, result, _tmp):
    result["summary"] = "{broken"


def _array_body(body, result, _tmp):
    result["summary"] = "[]"
    result["schema_valid"] = True


# defect -> mutation of the native result; the claimed verdict stays "valid" where the
# helper must reject on its own independent shape/material check.
_DEFECTS = {
    "partial": _set_verdict, "remaining": _add_remaining, "missing_field": _drop_summary,
    "exit_type": _bool_exit, "failed_check": _failing_exit, "missing_receipt": _drop_receipt,
    "empty_receipt": _empty_receipt, "no_checks": _no_checks, "not_declared": _undeclare,
    "empty_artifact": _empty_artifact, "schema_false": _schema_false,
    "schema_missing": _schema_absent, "lifecycle": _lifecycle, "bad_json": _bad_json,
    "wrong_shape": _array_body,
}


@pytest.mark.parametrize("defect", sorted(_DEFECTS))
def test_rejects_incomplete_results(checker, tmp_path, material, defect):
    result = envelope(copy.deepcopy(material))
    _DEFECTS[defect](copy.deepcopy(material), result, tmp_path)
    out = checker.check_result(result, required_artifacts=["review.md"], base_dir=tmp_path,
                               require_checks=True)
    assert out["accepted"] is False, (defect, out)
    assert out["errors"]


def test_custom_schema_cannot_relax_material_contract(checker, tmp_path, material):
    bad = envelope(material)
    bad["summary"] = "{}"
    out = checker.check_result(bad, schema={}, base_dir=tmp_path)
    assert out["accepted"] is False
    assert "body_schema_invalid" in out["errors"]
    tight = {"properties": {"summary": {"const": "Required summary"}}}
    out = checker.check_result(envelope(material), schema=tight, base_dir=tmp_path)
    assert out["accepted"] is False


def test_no_checks_and_native_fenced_json_keep_documented_scope(checker, tmp_path, material):
    material["checks"] = []
    result = envelope(material)
    result["summary"] = "```json\n" + result["summary"] + "\n```"
    out = checker.check_result(result, base_dir=tmp_path)
    assert out["accepted"] is True
    assert out["reported_check_count"] == 0
    assert "structure_and_materials_only" in out["scope"]
