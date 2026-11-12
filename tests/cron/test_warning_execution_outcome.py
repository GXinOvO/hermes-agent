"""Suppressed automatic diagnostics retain failed jobs without invented delivery receipts."""
import pytest

from cron import executions, incidents, jobs, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig


def delivery_queue_rows():
    from cron.delivery_queue import _connect

    conn = _connect()
    try:
        return [dict(r) for r in conn.execute("SELECT execution_id, status FROM deliveries")]
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["failure", "crash", "success"])
@pytest.mark.parametrize("suppress", [False, True])
@pytest.mark.parametrize("external_worker", [False, True])
def test_real_run_ledger_and_incident_match_actual_presentation(tmp_path, monkeypatch, mode, suppress, external_worker):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"display: {{suppress_warning_notifications: {str(suppress).lower()}}}\n"
    )
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token='loopback-only')
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    sent = []

    async def send(platform, pconfig, chat_id, text, **kwargs):
        sent.append(text)
        return {"success": True, "message_id": "receipt"}

    monkeypatch.setattr("tools.send_message_tool._send_to_platform", send)

    def run(job, **kwargs):
        if mode == "crash":
            raise RuntimeError("isolated provider failure")
        return (mode == "success", "retained raw output", "required result", None if mode == "success" else "isolated provider failure")

    monkeypatch.setattr(scheduler, "run_job", run)
    job = jobs.create_job(prompt="fixture only", schedule="every 1h", deliver="telegram:fixture")
    if external_worker:
        from cron import delivery_queue
        execution = executions.create_execution(job["id"], source="fixture")
        job["execution_id"] = execution["id"]
        monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution["id"])
        original_wait = delivery_queue.enqueue_and_wait

        def drain_before_wait(execution_id, job, content, *, for_failure=False):
            delivery_queue.enqueue(execution_id, job, content, for_failure=for_failure)
            assert scheduler.drain_delivery_queue({}, None) == 1
            return original_wait(execution_id, job, content, for_failure=for_failure)

        monkeypatch.setattr(delivery_queue, "enqueue_and_wait", drain_before_wait)
    scheduler.run_one_job(job)
    row = executions.latest_execution(job["id"])
    expected_suppression = suppress and mode != "success"
    # Every agent run (in-process or restart-safe worker) hands its send to the durable queue
    # (gateway-attached cron): the ledger books the handoff and the gateway's drain performs
    # (or suppresses) the send. Only a crash notice is sent inline.
    queued_lane = mode != "crash"
    if queued_lane:
        assert len(sent) == 0
        assert row["delivery_outcome"] == "queued"
        assert scheduler.drain_delivery_queue({}, None) == 1
        (queued,) = delivery_queue_rows()
        assert queued["status"] == ("suppressed" if expected_suppression else "delivered")
        row = executions.latest_execution(job["id"])  # the drain writes its outcome back
    assert len(sent) == (0 if expected_suppression else 1)
    assert row["delivery_outcome"] == ("suppressed" if expected_suppression else "delivered")
    saved = jobs.get_job(job["id"])
    assert saved["last_status"] == ("ok" if mode == "success" else "error")
    if mode != "success":
        assert "isolated provider failure" in saved["last_error"]
        incident = next(i for i in incidents.list_incidents() if i["job_id"] == job["id"])
        assert incident["state"] == ("detected" if suppress else "alerted")
    if mode != "crash":
        outputs = list((tmp_path / "cron" / "output" / job["id"]).glob("*.md"))
        assert outputs and "retained raw output" in outputs[0].read_text()


@pytest.mark.parametrize("timing", ["after_run", "before_bookkeeping", "during_timeout"])
@pytest.mark.parametrize("outcome", ["delivered", "unknown", "suppressed"])
def test_external_worker_crash_notice_remains_queued_until_delivery(tmp_path, monkeypatch, timing, outcome):
    from cron import delivery_queue

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"display: {{suppress_warning_notifications: {str(outcome == 'suppressed').lower()}}}\n"
    )
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="loopback-only")
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(delivery_queue, "DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS", 0)

    def crash(job, **kwargs):
        raise RuntimeError("isolated provider failure")

    sent = []

    async def send(platform, pconfig, chat_id, text, **kwargs):
        sent.append(text)
        return {"success": outcome != "unknown", "message_id": "receipt", "error": "refused"}

    monkeypatch.setattr(scheduler, "run_job", crash)
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", send)
    job = jobs.create_job(prompt="fixture only", schedule="every 1h", deliver="telegram:fixture")
    execution = executions.create_execution(job["id"], source="fixture")
    job["execution_id"] = execution["id"]
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution["id"])

    before_finish = []
    original_finish = scheduler.finish_execution
    if timing == "before_bookkeeping":
        def finish_after_drain(execution_id, **kwargs):
            before_finish.append(kwargs["delivery_outcome"])
            assert delivery_queue.get_status(execution_id)["status"] == "pending"
            assert scheduler.drain_delivery_queue({}, None) == 1
            return original_finish(execution_id, **kwargs)
        monkeypatch.setattr(scheduler, "finish_execution", finish_after_drain)
    elif timing == "during_timeout":
        original_timeout = delivery_queue._terminalize_wait_timeout

        def timeout_before_drain(execution_id):
            result = original_timeout(execution_id)
            assert result == ""
            assert delivery_queue.get_status(execution_id)["status"] == "pending"
            assert scheduler.drain_delivery_queue({}, None) == 1
            return result
        monkeypatch.setattr(delivery_queue, "_terminalize_wait_timeout", timeout_before_drain)

    assert scheduler.run_one_job(job) is False
    if timing == "after_run":
        assert delivery_queue.get_status(execution["id"])["status"] == "pending"
        assert executions.get_execution(execution["id"])["delivery_outcome"] == "queued"
        assert next(i for i in incidents.list_incidents() if i["job_id"] == job["id"])["state"] == "detected"
        assert not sent
        assert scheduler.drain_delivery_queue({}, None) == 1
    elif timing == "before_bookkeeping":
        assert before_finish == ["queued"]
    assert delivery_queue.get_status(execution["id"])["status"] == outcome
    assert executions.get_execution(execution["id"])["delivery_outcome"] == (
        "failed" if outcome == "unknown" else outcome)
    assert next(i for i in incidents.list_incidents() if i["job_id"] == job["id"])["state"] == (
        "alerted" if outcome == "delivered" else "detected")
    assert jobs.get_job(job["id"])["last_status"] == "error"
    assert len(sent) == (0 if outcome == "suppressed" else 1)
    assert scheduler.drain_delivery_queue({}, None) == 0
    assert len(sent) == (0 if outcome == "suppressed" else 1)
