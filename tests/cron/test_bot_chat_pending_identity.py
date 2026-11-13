"""A parked record's own payload is not part of its delivery identity.

Two failure notices composed for the same execution produce different text under one
delivery id. The record still describes that delivery — it is never handed off twice —
so a content difference must surface as the record's status, not as an identity
conflict that leaves the queue row pending forever. A genuinely different delivery
(job, execution, home, failure lane or seal) stays a hard conflict.
"""
import json
import os
from pathlib import Path

import pytest

from cron.bot_chat_legacy import pending_status
from cron.sealed_bot_result import delivery_identity


def _record(tmp_path, monkeypatch, **overrides):
    home = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    job = {"id": "job-1", "execution_id": "exec-1"}
    key = delivery_identity(home, job["id"], job["execution_id"], home)
    record = {"id": key, "status": "ambiguous", "job": dict(job), "content": "row text",
              "home": str(home), "for_failure": True, "sequence": 1}
    directory = home / "cron" / "bot_chat_pending"
    directory.mkdir(parents=True)
    path = directory / f"{key}.json"
    payload = dict(record, **overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return key, record, payload


def test_differently_composed_payload_returns_the_record_status(tmp_path, monkeypatch):
    key, record, _ = _record(tmp_path, monkeypatch)
    assert pending_status(key, record["job"], "another failure text", Path(record["home"]),
                          for_failure=True) == "ambiguous"


@pytest.mark.parametrize("drift", ["execution", "job", "failure", "home"])
def test_a_different_delivery_stays_a_conflict(tmp_path, monkeypatch, drift):
    key, record, payload = _record(tmp_path, monkeypatch)
    job = dict(record["job"])
    if drift == "execution":
        job["execution_id"] = "exec-2"
    elif drift == "job":
        job["id"] = "job-2"
    elif drift == "failure":
        payload["for_failure"] = False
        (Path(payload["home"]) / "cron" / "bot_chat_pending" / f"{key}.json").write_text(
            json.dumps(payload), encoding="utf-8")
    else:
        payload["home"] = str(Path(payload["home"]) / "elsewhere")
        (Path(record["home"]) / "cron" / "bot_chat_pending" / f"{key}.json").write_text(
            json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        pending_status(key, job, record["content"], Path(record["home"]), for_failure=True)
