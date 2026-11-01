#!/usr/bin/env python3
"""Validate a paired full-context vs compressed-task evaluation.

This is an offline gate. It never calls a model and treats missing controls,
route drift, missing task artifacts, or unverifiable hashes as blocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROUTE_KEYS = (
    "provider",
    "requested_model",
    "api_mode",
    "endpoint_sha256",
    "reasoning_effort",
    "sdk_retries",
    "max_output_tokens",
    "timeout_seconds",
    "stream",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def route_from(run: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    route = dict(preflight.get("route") or {})
    route.update({key: preflight[key] for key in ROUTE_KEYS if key in preflight})
    route.update({key: run[key] for key in ROUTE_KEYS if key in run})
    return {key: route.get(key) for key in ROUTE_KEYS}


def validate_route(route: dict[str, Any], label: str, reasons: list[str]) -> None:
    missing = [key for key in ROUTE_KEYS if not isinstance(route.get(key), (bool, int)) and not str(route.get(key) or "").strip()]
    if missing:
        reasons.append(f"{label}: route is missing required fields: {', '.join(missing)}")


def validate_route_sources(run: dict[str, Any], preflight: dict[str, Any], label: str, reasons: list[str]) -> None:
    nested = preflight.get("route")
    nested = nested if isinstance(nested, dict) else {}
    for key in ROUTE_KEYS:
        values = [source[key] for source in (nested, preflight, run) if key in source]
        if len(set(map(str, values))) > 1:
            reasons.append(f"{label}: route field has conflicting sources: {key}")


def validate_artifact(run_dir: Path, result: dict[str, Any], label: str, reasons: list[str]) -> dict[str, Any]:
    artifact = result.get("artifact_path")
    claimed_hash = result.get("artifact_sha256")
    if not isinstance(artifact, str) or not artifact:
        reasons.append(f"{label}: task-result.json has no artifact_path")
        return {"status": "missing"}
    path = Path(artifact)
    if not path.is_absolute():
        path = run_dir / path
    try:
        path = path.resolve()
        path.relative_to(run_dir.resolve())
    except ValueError:
        reasons.append(f"{label}: artifact must be inside its run directory")
        return {"status": "invalid", "path": str(path)}
    if not path.is_file():
        reasons.append(f"{label}: artifact does not exist: {path}")
        return {"status": "missing", "path": str(path)}
    actual_hash = sha256(path)
    if not isinstance(claimed_hash, str) or claimed_hash != actual_hash:
        reasons.append(f"{label}: artifact hash is absent or does not match")
    if result.get("status") != "accepted":
        reasons.append(f"{label}: task result is not accepted")
    tests_failed = result.get("tests_failed")
    if isinstance(tests_failed, bool) or tests_failed not in (0, []):
        reasons.append(f"{label}: task result reports failed tests")
    constraints = result.get("constraints_checked")
    if (
        not isinstance(constraints, list)
        or not constraints
        or not all(isinstance(item, str) and item.strip() for item in constraints)
    ):
        reasons.append(f"{label}: no constraints_checked evidence")
    return {"status": result.get("status"), "path": str(path), "sha256": actual_hash}


def validate_run(root: Path, run_dir: Path, label: str, reasons: list[str]) -> dict[str, Any]:
    if not run_dir.is_dir():
        reasons.append(f"{label}: run directory is missing")
        return {"status": "missing"}
    receipt_path = run_dir / "run-receipt.json"
    preflight_path = run_dir / "preflight.json"
    result_path = run_dir / "task-result.json"
    if not receipt_path.is_file():
        reasons.append(f"{label}: run-receipt.json is missing")
        return {"status": "missing"}
    if not preflight_path.is_file():
        reasons.append(f"{label}: preflight.json is missing")
        return {"status": "missing"}
    receipt = read_json(receipt_path)
    preflight = read_json(preflight_path)
    validate_route_sources(receipt, preflight, label, reasons)
    route = route_from(receipt, preflight)
    validate_route(route, label, reasons)
    for key in ROUTE_KEYS:
        if key in receipt and key in preflight and receipt[key] != preflight[key]:
            reasons.append(f"{label}: receipt/preflight route field differs: {key}")
    root_input = root / "frozen-input.json"
    expected_input = sha256(root_input) if root_input.is_file() else None
    preflight_input = preflight.get("input_sha256")
    receipt_input = receipt.get("input_sha256")
    if preflight_input != receipt_input:
        reasons.append(f"{label}: receipt/preflight input hashes differ")
    actual_input = preflight_input or receipt_input
    if expected_input is None:
        reasons.append("root: frozen-input.json is missing")
    elif actual_input != expected_input:
        reasons.append(f"{label}: input hash does not match frozen-input.json")
    if receipt.get("status") not in {"completed", "returned"}:
        reasons.append(f"{label}: run status is not completed/returned")
    if not result_path.is_file():
        reasons.append(f"{label}: task-result.json is missing; summary JSON is not task execution")
        artifact = {"status": "missing"}
    else:
        artifact = validate_artifact(run_dir, read_json(result_path), label, reasons)
    return {
        "status": receipt.get("status"),
        "route": route,
        "input_sha256": actual_input,
        "artifact": artifact,
        "run_dir": str(run_dir.resolve()),
        "receipt": str(receipt_path),
        "task_result": str(result_path) if result_path.is_file() else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--compressed", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    reasons: list[str] = []
    if args.control.resolve() == args.compressed.resolve():
        reasons.append("control/compressed: run directories must be distinct")
    control = validate_run(args.root, args.control, "control", reasons)
    compressed = validate_run(args.root, args.compressed, "compressed", reasons)
    if control.get("route") != compressed.get("route") and control.get("route") != "missing" and compressed.get("route") != "missing":
        reasons.append("control/compressed: route or protocol settings differ")
    if control.get("input_sha256") != compressed.get("input_sha256"):
        reasons.append("control/compressed: input hashes differ")
    control_artifact = control.get("artifact", {}).get("path")
    compressed_artifact = compressed.get("artifact", {}).get("path")
    if control_artifact and control_artifact == compressed_artifact:
        reasons.append("control/compressed: task artifacts must be distinct")
    result = {
        "status": "pass" if not reasons else "blocked",
        "reasons": reasons,
        "control": control,
        "compressed": compressed,
        "comparison": {
            "same_frozen_input": control.get("input_sha256") == compressed.get("input_sha256")
            and bool(control.get("input_sha256")),
            "same_route_and_protocol": control.get("route") == compressed.get("route")
            and control.get("route") not in (None, "missing"),
            "both_accepted_task_artifacts": all(
                run.get("artifact", {}).get("status") == "accepted" for run in (control, compressed)
            ),
        },
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
