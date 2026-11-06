---
title: Background completion backlogs
sidebar_label: Completion backlogs
---

# Background completion backlogs

Interactive surfaces coalesce consecutive background-process completions that are
already ready for one conversation into a single notification turn. This does not
add a delay or promise to combine jobs that finish at different times. Failures and
successful outputs remain in the batch; one completion keeps its original text.

Process identity remains available until dispatch. Explicit `process_manage`
wait/log/kill consumption can therefore suppress a CLI completion even after it
has left the process registry and entered the input queue. An entirely consumed
batch starts no turn. Watching output and async-delegation results remain separate
notifications, in their original order; they are not folded into completion batches.

## Consumers and ownership

- **Classic CLI:** `hermes_cli/cli_process_notifications.py` owns the idle/post-turn
  drain, compression-aware ownership and final input unwrapping.
- **TUI and Desktop:** `tui_gateway/session_notifications.py` groups the poller's
  ready snapshot after checking ownership. Each process still emits its own UI
  status. Busy sessions requeue structured events, not rendered batch strings.
- **Post-turn TUI safety net:** `tui_gateway/prompt_turn.py` uses the same routing
  and rendering path. Desktop and dashboard chat clients share this backend.
- **Messaging gateway:** `gateway/run_notifications.py` already uses its own
  route-keyed short-window batching. This interactive-backlog change does not
  replace that mechanism or alter adapter sends.
- **Noninteractive/headless consumers:** this change does not create a new
  autonomous notification loop for an API request, ACP client, or one-shot CLI.

The shared renderer is `tools/process_registry_notifications.py::ProcessNotificationBatch`.
Neither a batch nor its delivery status is persisted into the system prompt.
Addressed events still require a provable owner; another live session cannot adopt
them. Delegation delivery continues through its existing durable claim/complete
ledger, once per delegation rather than once per process batch.

## Child-process visibility and escalation

TUI/Desktop render process status updates after ownership and consumed-output checks,
separately from deciding whether a notification starts a model turn. By default a
subagent's ordinary completion, nonzero command exit, heartbeat or watch event updates
status without waking the parent. The child's own process polling and log reads remain
available. `delegation.surface_child_process_notifications: true` enables those parent
wakes; `display.background_process_notifications: off` still disables process wakes.
These controls do not mute final delegation results.

A child process marked `lost` or `failed_start` can wake the parent without waiting for
the child to finish, subject to the process-wake preference. `failed_start` is supported
when an event carries that reason; ordinary spawn exceptions still return directly to
the child's tool call and do not gain a new notification producer here. Existing
delegation failure and timeout results also reach the parent. In a parallel batch, a child that exhausts
its iteration budget now sends an early incomplete-result notice, including its partial
summary, while siblings continue; that notice does not consume the final batch result.
The existing inactivity monitor handles stalled delegation results; this change does not
shorten its thresholds or add semantic detection of repeated unsuccessful work.

Status presentation still honors diagnostic-display preferences. Restored status events
are not model responses or a new persistent chat message. This policy does not promise
that every application-level blocker can be inferred from a command exit code or text.

## Local validation and its limits

`evals/completion_backlog_probe.py REPO OUTPUT.json` starts real local shell children
in temporary directories, reads their real completion events, and drives the
production CLI, TUI poller and post-turn notification routes. A loopback HTTP turn
sink replaces `chat` / `_run_prompt_submit`; it records actual dispatches but does
**not** exercise model inference, native renderer interaction or a hosted platform.
Synthetic watch and delegation envelopes are labeled fixtures; delegation claims
use the real temporary SQLite ledger. The backlog case includes a nonzero exit.

The probe checks a ready backlog, a single exact payload, explicitly consumed
results, foreign ownership, and watches/delegations interleaved with completions.
It measures turn admission at the notification boundary, not model token savings.
