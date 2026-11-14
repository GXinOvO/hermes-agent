"""Inject the compatibility router into queue tests for its legacy transports.

The production scheduler drain uses cron.strict_delivery. Legacy native fallback,
relay upload retry and registry standalone-sender contracts belong to the older
_deliver_result router, which the queue's public deliver callback can still drive.
"""
from cron import delivery_queue, scheduler_delivery


def drain_compatibility_router(adapters, loop):
    def send(job, content, for_failure):
        error = scheduler_delivery._deliver_result(
            job, content, adapters=adapters, loop=loop, for_failure=for_failure)
        if not error and not job.get("_notification_all_targets_suppressed") and for_failure:
            from cron import scheduler
            scheduler._mark_incident_alerted(job.get("_failure_incident_id"))
        return error

    return delivery_queue.drain(send)
