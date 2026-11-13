"""Monitor-mode cron support — hash-suppressed change detection.

A monitor job attaches a cheap source (``monitor_script`` / ``monitor_url``) to an LLM cron job.
Each tick runs the source FIRST and hashes its EXACT output bytes (no timestamp/whitespace
normalization — scripts must emit stable output) against the hash from the last agent-triggering
tick: unchanged → agent run suppressed (silent ``no_change`` run); changed/first run → a "MONITOR
CHANGE DETECTED" block (capped unified diff + new output) is injected into the prompt; source
failure → an ERROR, never a change, and the stored hash is left untouched. State:
``job["monitor_state"]`` in jobs.json (hash + last_changed_at) and
``OUTPUT_DIR/<job_id>/monitor_last_output.txt`` (for the diff).
"""

from __future__ import annotations

import difflib
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Prompt-injection caps: unified diff, and new-output block (mirrors the 8k context_from truncation
# in cron/scheduler.py). Then bounded-GET limits for monitor_url sources.
MAX_DIFF_CHARS = 4000
MAX_OUTPUT_CHARS = 8000
URL_TIMEOUT_SECONDS = 30
MAX_URL_BYTES = 262_144  # 256 KiB
MAX_URL_REDIRECTS = 5

_SNAPSHOT_FILENAME = "monitor_last_output.txt"


@dataclass
class MonitorOutcome:
    """Result of one monitor-source evaluation."""

    ok: bool
    changed: bool = False
    first_run: bool = False
    context_block: Optional[str] = None
    error: Optional[str] = None
    analysis_status: Optional[str] = None


def hash_monitor_output(output: str) -> str:
    """Hash the monitor output as exact UTF-8 bytes (no normalization)."""
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def build_monitor_diff(old: str, new: str) -> str:
    """Unified diff of old vs new monitor output, capped at MAX_DIFF_CHARS."""
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="previous", tofile="current", lineterm="",
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return diff


def _snapshot_path(job_id: str):
    from cron.jobs import _job_output_dir

    return _job_output_dir(job_id) / _SNAPSHOT_FILENAME


def _read_last_output(job_id: str) -> str:
    try:
        path = _snapshot_path(job_id)
        if path.exists():
            return path.read_text(encoding="utf-8-sig")
    except Exception as exc:
        logger.warning("Monitor: failed to read last output for %r: %s", job_id, exc)
    return ""


def _write_last_output(job_id: str, output: str) -> None:
    try:
        from cron.jobs import _ensure_cron_dir

        path = _snapshot_path(job_id)
        _ensure_cron_dir(path.parent)
        path.write_text(output, encoding="utf-8")
    except Exception as exc:
        logger.warning("Monitor: failed to persist last output for %r: %s", job_id, exc)


def _fetch_monitor_url(url: str) -> tuple[bool, str]:
    """Bounded GET of a monitor URL. Returns (ok, body-or-error).

    The URL can come from the agent's own cronjob tool call and the body is fed back into the
    agent's prompt, so every hop (initial URL and each redirect target) passes the shared SSRF
    policy, and the connection itself goes through the connect-time-guarded client.
    """
    from urllib.parse import urljoin

    from tools.url_safety import create_ssrf_safe_client, is_safe_url

    current_url = str(url)
    if not current_url.lower().startswith(("http://", "https://")):
        return False, f"monitor_url must be http(s): {url!r}"
    try:
        with create_ssrf_safe_client(timeout=URL_TIMEOUT_SECONDS, follow_redirects=False,
                                     headers={"User-Agent": "hermes-cron-monitor",
                                              "Accept-Encoding": "identity"}) as client:
            for _ in range(MAX_URL_REDIRECTS + 1):
                if not is_safe_url(current_url):
                    return False, ("monitor_url blocked: private/internal or unsafe destination "
                                   "(intranet monitors need security.allow_private_urls: true)")
                with client.stream("GET", current_url) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return False, "monitor_url redirect without a Location header"
                        current_url = urljoin(current_url, location)
                        continue
                    resp.raise_for_status()
                    body = bytearray()
                    # Raw bytes, as urllib read them: decoding happens before this cap sees a
                    # chunk, and one read of a hostile br/gzip body inflates to GBs.
                    for chunk in resp.iter_raw():
                        body += chunk
                        if len(body) > MAX_URL_BYTES:
                            break
                return True, bytes(body[:MAX_URL_BYTES]).decode("utf-8", errors="replace")
        return False, f"monitor_url exceeded {MAX_URL_REDIRECTS} redirects"
    except Exception as exc:
        return False, f"monitor_url fetch failed: {exc}"


def _field(job: dict, key: str) -> str:
    return (job.get(key) or "").strip()


def _run_monitor_source(job: dict) -> tuple[bool, str]:
    """Run the job's monitor source (script or URL). Returns (ok, output)."""
    monitor_script = _field(job, "monitor_script")
    if monitor_script:
        # Same containment + interpreter rules as the existing `script` field.
        from cron.scheduler_script import _run_job_script

        return _run_job_script(monitor_script, workdir=_field(job, "workdir") or None,
                               interpreter=job.get("interpreter"))
    monitor_url = _field(job, "monitor_url")
    if monitor_url:
        return _fetch_monitor_url(monitor_url)
    return False, "monitor job has neither monitor_script nor monitor_url"


def job_has_monitor(job: dict) -> bool:
    return bool(_field(job, "monitor_script") or _field(job, "monitor_url"))


def check_monitor(job: dict) -> MonitorOutcome:
    """Run the monitor source and decide whether the agent should run.

    On change (or first run) the new hash + snapshot are persisted BEFORE the agent runs — detection
    time is the state boundary, so a failed agent run doesn't re-alert on the same content forever.
    On failure nothing is persisted.
    """
    job_id = str(job.get("id") or "")
    ok, output = _run_monitor_source(job)
    if not ok:
        return MonitorOutcome(ok=False, error=output)

    new_hash = hash_monitor_output(output)
    raw_state = _refresh_observation(job)
    last_hash = raw_state.get("last_output_hash") if isinstance(raw_state, dict) else None

    if last_hash is not None and new_hash == last_hash:
        status = (raw_state.get("observation") or {}).get("analysis_status", "unverified")
        return MonitorOutcome(ok=True, changed=False, analysis_status=status)

    first_run = last_hash is None
    old_output = "" if first_run else _read_last_output(job_id)

    shown_output = output
    if len(shown_output) > MAX_OUTPUT_CHARS:
        shown_output = shown_output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"

    current = f"### Current output\n\n```\n{shown_output}\n```"
    if first_run:
        context_block = (
            "## Monitor Baseline (first run)\n\n"
            "This is the first observation of the monitored source — there is "
            "no previous output to diff against.\n\n" + current
        )
    else:
        diff = build_monitor_diff(old_output, output)
        context_block = (
            "## MONITOR CHANGE DETECTED\n\n"
            "The monitored source's output changed since the last run.\n\n"
            f"### Diff (previous → current)\n\n```diff\n{diff}\n```\n\n" + current
        )

    _persist_monitor_state(job, new_hash, output)
    return MonitorOutcome(ok=True, changed=True, first_run=first_run, context_block=context_block)


def _refresh_observation(job: dict) -> dict:
    """A dead analysis remains unknown even when an unchanged tick is suppressed."""
    from cron.executions import execution_owner_live, get_execution
    from cron.jobs import _jobs_lock, get_job, update_job

    with _jobs_lock():
        current = get_job(job['id']) or job
        state = dict(current.get('monitor_state') or {})
        observation = dict(state.get('observation') or {})
        execution_id = observation.get('execution_id')
        if execution_id and observation.get('analysis_status') == 'pending':
            row = get_execution(execution_id)
            if row and row['job_id'] == job['id'] and row['status'] in {'completed', 'failed', 'cancelled'}:
                observation['analysis_status'] = row['status']
            elif not execution_owner_live(execution_id, job['id']):
                observation['analysis_status'] = 'unknown'
            state['observation'] = observation
            state['observations'] = dict(state.get('observations') or {}, **{execution_id: observation})
            update_job(job['id'], {'monitor_state': state})
        return state


def _persist_monitor_state(job: dict, new_hash: str, output: str) -> None:
    from cron.jobs import _hermes_now, _jobs_lock, get_job, update_job

    job_id = job['id']
    _write_last_output(job_id, output)
    # Observation must be durable before analysis can perform tools. Never roll this hash back
    # to retry an uncertain analysis. The execution ledger and its result own that uncertainty.
    with _jobs_lock():
        current = get_job(job_id) or job
        state = dict(current.get('monitor_state') or {})
        observed_at = _hermes_now().isoformat()
        execution_id = job.get('execution_id')
        observation = {'output_hash': new_hash, 'execution_id': execution_id,
                       'observed_at': observed_at, 'analysis_status': 'pending' if execution_id else 'unknown',
                       'consumed': False}
        observations = {key: value for key, value in (state.get('observations') or {}).items()
                        if not value.get('consumed')}
        observations[execution_id or observed_at] = observation
        state.update(last_output_hash=new_hash, last_changed_at=observed_at,
                     observation=observation, observations=observations)
        update_job(job_id, {'monitor_state': state})


def settle_observation(job: dict, execution_id: str, *, success: bool, delivery_outcome) -> None:
    """A completed result's tail consumed this observation, not a later unchanged tick."""
    from cron.jobs import _hermes_now, _jobs_lock, get_job, update_job
    from cron.scheduler_results import recovery_job_matches

    with _jobs_lock():
        current = get_job(job['id'])
        if current is None or not recovery_job_matches({'job': job}, current):
            return
        state = dict(current.get('monitor_state') or {})
        observations = dict(state.get('observations') or {})
        observation = dict(observations.get(execution_id) or {})
        if not observation or observation.get('consumed'):
            return
        observation.update(analysis_status='completed' if success else 'failed', consumed=True,
                           consumed_at=_hermes_now().isoformat(), delivery_outcome=delivery_outcome)
        observations[execution_id] = observation
        state['observations'] = observations
        if (state.get('observation') or {}).get('execution_id') == execution_id:
            state['observation'] = observation
            if success:
                state['processed'] = observation
        update_job(job['id'], {'monitor_state': state})
