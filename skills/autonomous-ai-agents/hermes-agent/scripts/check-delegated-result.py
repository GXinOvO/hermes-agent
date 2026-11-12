"""Check a native delegated result's structure and required material, not business truth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_source = Path(__file__).resolve().parents[4]
if (_source / "tools" / "delegation_output_schema.py").is_file():
    sys.path.insert(0, str(_source))

import jsonschema
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from tools.delegation_output_schema import extract_json_candidate

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "delegation-output.schema.json"
SCOPE = "structure_and_materials_only; parent must verify evidence and original acceptance"


def _body(result, schema):
    errors = []
    if not isinstance(result, dict):
        return None, ["result_not_object"]
    if result.get("status") != "completed":
        errors.append("lifecycle_not_completed")
    if result.get("truncated") is True or result.get("exit_reason") == "max_iterations":
        errors.append("lifecycle_incomplete")
    if result.get("schema_valid") is not True:
        errors.append("schema_not_validated")
    summary = result.get("summary")
    if not isinstance(summary, str):
        return None, errors + ["summary_not_string"]
    try:
        body = json.loads(extract_json_candidate(summary))
    except ValueError:
        return None, errors + ["summary_not_json"]
    contract = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
    shapes = [contract] if schema is None else [contract, schema]
    for shape in shapes:
        cls = validator_for(shape)
        cls.check_schema(shape)
        if not cls(shape).is_valid(body):
            return None, errors + ["body_schema_invalid"]
    return body, errors


def _path(value, base):
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _nonempty_file(path):
    return path.is_file() and path.stat().st_size > 0


def _artifacts(body, required, base):
    artifacts = {_path(value, base) for value in body["artifacts"]}
    errors = ["declared_artifact_missing_or_empty:" + str(path)
              for path in sorted(artifacts) if not _nonempty_file(path)]
    for value in required:
        path = _path(value, base)
        if not _nonempty_file(path):
            errors.append("required_artifact_missing:" + str(path))
        if path not in artifacts:
            errors.append("required_artifact_not_declared:" + str(path))
    return len(artifacts), errors


def _checks(body, base, require_checks):
    errors = ["required_checks_missing"] if require_checks and not body["checks"] else []
    for check in body["checks"]:
        if check["exit_code"] != 0:
            errors.append("reported_check_failed:" + check["command"])
        path = _path(check["receipt"], base)
        if not _nonempty_file(path):
            errors.append("check_receipt_missing_or_empty:" + str(path))
    return errors


def check_result(result, schema=None, required_artifacts=(), base_dir=None, require_checks=False):
    """A custom schema can tighten, but never relax, the six-field material contract."""
    body, errors = _body(result, schema)
    if body is None:
        return {"accepted": False, "errors": errors, "scope": SCOPE}
    if body["verdict"] != "pass":
        errors.append("child_not_pass")
    if body["remaining"]:
        errors.append("remaining_work")
    base = Path(base_dir or Path.cwd())
    count, artifact_errors = _artifacts(body, required_artifacts, base)
    errors.extend(artifact_errors)
    errors.extend(_checks(body, base, require_checks))
    return {"accepted": not errors, "errors": errors, "artifact_count": count,
            "reported_check_count": len(body["checks"]), "scope": SCOPE}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--schema", type=Path, help="Additional task-specific material constraints")
    parser.add_argument("--required-artifact", action="append", default=[])
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--require-checks", action="store_true")
    args = parser.parse_args(argv)
    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8")) if args.schema else None
        result = check_result(json.loads(args.result.read_text(encoding="utf-8")), schema,
                              args.required_artifact, args.base_dir or args.result.parent, args.require_checks)
    except (OSError, ValueError, SchemaError) as exc:
        result = {"accepted": False, "errors": [type(exc).__name__ + ": " + str(exc)], "scope": SCOPE}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
