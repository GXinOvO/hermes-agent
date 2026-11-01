"""Behavioral contract tests for the offline paired-task acceptance gate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


_VALID_ROUTE = {
    "provider": "custom",
    "requested_model": "model-a",
    "api_mode": "chat_completions",
    "endpoint_sha256": "a" * 64,
    "reasoning_effort": "max",
    "sdk_retries": 0,
    "max_output_tokens": 1000,
    "timeout_seconds": 120,
    "stream": False,
}


def _validator_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "validate_paired_task.py"
    spec = importlib.util.spec_from_file_location("paired_task_validator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_run(root: Path, name: str, *, route=None, input_hash=None, constraints=None, artifact_name=None):
    run_dir = root / name
    run_dir.mkdir()
    artifact = run_dir / (artifact_name or f"{name}.txt")
    artifact.write_text(f"accepted artifact for {name}\n")
    actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    route = dict(_VALID_ROUTE if route is None else route)
    input_hash = input_hash or hashlib.sha256((root / "frozen-input.json").read_bytes()).hexdigest()
    preflight = {**route, "input_sha256": input_hash}
    receipt = {**route, "input_sha256": input_hash, "status": "completed"}
    result = {
        "status": "accepted",
        "artifact_path": str(artifact),
        "artifact_sha256": actual_hash,
        "tests_failed": 0,
        "constraints_checked": ["same_frozen_input"] if constraints is None else constraints,
    }
    (run_dir / "preflight.json").write_text(json.dumps(preflight))
    (run_dir / "run-receipt.json").write_text(json.dumps(receipt))
    (run_dir / "task-result.json").write_text(json.dumps(result))
    return run_dir


def _invoke(module, root: Path, control: Path, compressed: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["validate_paired_task.py", "--root", str(root), "--control", str(control), "--compressed", str(compressed)],
    )
    code = module.main()
    output = capsys.readouterr().out
    return code, json.loads(output)


@pytest.mark.parametrize("nested_preflight", [False, True])
def test_validator_accepts_only_complete_distinct_paired_evidence(tmp_path, monkeypatch, capsys, nested_preflight):
    module = _validator_module()
    (tmp_path / "frozen-input.json").write_text("frozen input\n")
    control = _write_run(tmp_path, "control")
    compressed = _write_run(tmp_path, "compressed")

    if nested_preflight:
        for run_dir in (control, compressed):
            path = run_dir / "preflight.json"
            preflight = json.loads(path.read_text())
            path.write_text(json.dumps({
                "route": {key: preflight[key] for key in _VALID_ROUTE},
                "input_sha256": preflight["input_sha256"],
            }))

    code, result = _invoke(module, tmp_path, control, compressed, monkeypatch, capsys)

    assert code == 0
    assert result["status"] == "pass"
    assert result["comparison"]["same_route_and_protocol"] is True


@pytest.mark.parametrize(
    "mutation", [
        "missing_route", "stream_drift", "false_constraint", "input_conflict", "outside_artifact",
        "missing_tests_failed", "nested_route_conflict", "nested_only_conflict", "empty_route", "both_empty_route",
    ]
)
def test_validator_blocks_degenerate_pairings(tmp_path, monkeypatch, capsys, mutation):
    module = _validator_module()
    (tmp_path / "frozen-input.json").write_text("frozen input\n")
    control = _write_run(tmp_path, "control")
    route = dict(_VALID_ROUTE)
    constraints = None
    input_hash = None
    def missing_route():
        route.pop("provider")

    def stream_drift():
        route["stream"] = True

    def false_constraint():
        nonlocal constraints
        constraints = [False]

    def input_conflict():
        nonlocal input_hash
        input_hash = "b" * 64

    def outside_artifact():
        pass

    def missing_tests_failed():
        pass

    def nested_route_conflict():
        pass

    def nested_only_conflict():
        pass

    def empty_route():
        route.update({key: "" for key in ("provider", "requested_model", "api_mode", "endpoint_sha256")})

    def both_empty_route():
        empty_route()
        for name in ("preflight.json", "run-receipt.json"):
            path = control / name
            record = json.loads(path.read_text())
            record.update({key: "" for key in ("provider", "requested_model", "api_mode", "endpoint_sha256")})
            path.write_text(json.dumps(record))

    {
        "missing_route": missing_route,
        "stream_drift": stream_drift,
        "false_constraint": false_constraint,
        "input_conflict": input_conflict,
        "outside_artifact": outside_artifact,
        "missing_tests_failed": missing_tests_failed,
        "nested_route_conflict": nested_route_conflict,
        "nested_only_conflict": nested_only_conflict,
        "empty_route": empty_route,
        "both_empty_route": both_empty_route,
    }[mutation]()
    compressed = _write_run(
        tmp_path,
        "compressed",
        route=route,
        constraints=constraints,
        input_hash=input_hash,
    )
    def edit_input_conflict():
        receipt = json.loads((compressed / "run-receipt.json").read_text())
        receipt["input_sha256"] = "c" * 64
        (compressed / "run-receipt.json").write_text(json.dumps(receipt))

    def edit_outside_artifact():
        outside = tmp_path / "outside-artifact.txt"
        outside.write_text("outside\n")
        result = json.loads((compressed / "task-result.json").read_text())
        result["artifact_path"] = str(outside)
        result["artifact_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        (compressed / "task-result.json").write_text(json.dumps(result))

    def edit_missing_tests_failed():
        result = json.loads((compressed / "task-result.json").read_text())
        result.pop("tests_failed")
        (compressed / "task-result.json").write_text(json.dumps(result))

    def edit_nested_route_conflict():
        preflight = json.loads((compressed / "preflight.json").read_text())
        nested = {key: preflight[key] for key in _VALID_ROUTE}
        nested["api_mode"] = "anthropic_messages"
        preflight["route"] = nested
        (compressed / "preflight.json").write_text(json.dumps(preflight))

    def edit_nested_only_conflict():
        path = compressed / "preflight.json"
        preflight = json.loads(path.read_text())
        nested = {key: preflight[key] for key in _VALID_ROUTE}
        nested["api_mode"] = "anthropic_messages"
        path.write_text(json.dumps({"route": nested, "input_sha256": preflight["input_sha256"]}))

    {
        "input_conflict": edit_input_conflict,
        "outside_artifact": edit_outside_artifact,
        "missing_tests_failed": edit_missing_tests_failed,
        "nested_route_conflict": edit_nested_route_conflict,
        "nested_only_conflict": edit_nested_only_conflict,
    }.get(mutation, lambda: None)()

    code, result = _invoke(module, tmp_path, control, compressed, monkeypatch, capsys)

    assert code == 2
    assert result["status"] == "blocked"


def test_validator_blocks_same_run_directory(tmp_path, monkeypatch, capsys):
    module = _validator_module()
    (tmp_path / "frozen-input.json").write_text("frozen input\n")
    run_dir = _write_run(tmp_path, "same")

    code, result = _invoke(module, tmp_path, run_dir, run_dir, monkeypatch, capsys)

    assert code == 2
    assert any("distinct" in reason for reason in result["reasons"])
