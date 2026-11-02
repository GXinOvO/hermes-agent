"""Read-only full integrity verification of an explicitly selected offline copy."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def _positive_seconds(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
    return value


def add_verify_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "verify", help="Fully check an offline session database copy without restoring it",
        description="Run full SQLite integrity_check on a consistent offline backup copy. "
                    "No automatic repair or restore. Do not copy an active WAL database with cp.")
    parser.add_argument("--source", type=Path, required=True, help="Offline database backup to check")
    parser.add_argument("--budget-seconds", type=_positive_seconds, default=300.0,
                        help="Cooperative SQL time budget (default 300); blocked filesystem I/O can overrun it")
    parser.add_argument("--json", action="store_true", help="Machine-readable result without progress messages")


def _offline_refusal(source: Path) -> str | None:
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from hermes_state_holders import foreign_state_db_holders
    from hermes_cli.sqlite_safe_read import read_header_bytes_preopen

    root = get_default_hermes_root().resolve()
    active = {(get_hermes_home() / "state.db").resolve(), root / "state.db"}
    profiles = root / "profiles"
    if profiles.is_dir():
        active.update(p / "state.db" for p in profiles.iterdir() if p.is_dir())
    for path in active:
        if source == path or (source.exists() and path.exists() and source.samefile(path)):
            return "active profile database selected; supply a consistent offline backup copy"
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        return "database sidecars exist; supply a closed, consistent backup without active WAL/journal files"
    if foreign_state_db_holders(source):
        return "another process holds the source, or its offline state cannot be proven"
    if source.is_file():
        header = read_header_bytes_preopen(source, length=20)
        if header is not None and header.startswith(b"SQLite format 3\0") and header[18:20] != b"\x01\x01":
            return "WAL-mode file selected; use a consistent backup in DELETE journal mode"
    return None


def cmd_verify(args) -> int:
    from hermes_cli.state_integrity import check_database
    from hermes_cli.update_progress import maintenance_stage

    source = args.source.expanduser().resolve()
    try:
        reason = _offline_refusal(source)
    except (OSError, RuntimeError) as exc:
        reason = f"cannot prove source is an offline backup: {exc}"
    if reason:
        result = {"level": "full", "status": "incomplete", "full_check": "incomplete", "message": reason}
    elif args.json:
        result = check_database(source, full=True, budget_seconds=args.budget_seconds)
    else:
        with maintenance_stage("Full integrity check of offline backup"):
            result = check_database(source, full=True, budget_seconds=args.budget_seconds)
    if result["status"] == "verified":
        # Recheck admission after the scan: a new WAL/holder means this was not
        # the unused backup copy whose full-check receipt we promised.
        try:
            changed = _offline_refusal(source)
        except (OSError, RuntimeError) as exc:
            changed = f"cannot recheck offline source: {exc}"
        if changed:
            result.update(status="incomplete", full_check="incomplete", message=changed)
    if args.json:
        print(json.dumps(result))
    else:
        marker = "✓" if result["status"] == "verified" else "⚠"
        print(f"{marker} {result['message']}")
        print(f"  level={result['level']}, status={result['status']}, full_check={result['full_check']}")
    return 0 if result["status"] == "verified" else 1
