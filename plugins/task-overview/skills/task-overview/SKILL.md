---
name: task-overview
description: Use when updating a task card. Record evidence and gaps.
version: 0.1.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
---

# Task overview records

## When to Use

Use when the owner asks to update the session task card, task status, outstanding work or acceptance. This plugin adds a `::task` transcript directive and a read-only `task-overview-read` slash command; no tool automatically accepts work.

## Procedure

1. Read the original objective, latest requirements, actual todo results and material evidence. Preserve the original acceptance scope. Keep required outstanding work separate from deferred suggestions and owner decisions.
2. Emit one directive at column zero on its own line in the normal visible assistant reply. Do not put it in a code block, quote, tool arguments or another role. Use double-quoted JSON string values, at most 2048 characters inside braces, with no nested braces in values.
3. Fields: `title`, `goal`, `result`, `remaining`, `deferred`, `decision`; multi-item text uses `\n`. For each stage (`implementation`, `tests`, `review`, `integration`, `activation`), choose `passed`, `failed`, `pending` or `not_applicable`, and cite evidence in `<stage>_evidence`. A passed or not-applicable stage without an evidence reference will display unverified.
4. Describe the evidence ceiling. Agent records display as registered, not independent acceptance. A passed test is scoped to that exact run; source changes, integration and live activation are separate facts. Unknown evidence stays pending. Do not close all work merely because a todo list says completed.
5. Update after substantive requirements, execution or edits. Current frontend conservatively expires old records after execution tools. External changes are not fingerprinted; re-check before claiming freshness.

Example shape (literal documentation only):

```text
::task{title="修复导出" goal="通过原始验收" result="实现和限定测试通过" remaining="桌面安装和实际交互" deferred="跨平台试用" decision="无" implementation="passed" implementation_evidence="src/export.py:42" tests="passed" tests_evidence="evidence/test-run.json" review="pending" integration="pending" activation="pending"}
```

## Pitfalls

- The original goal and latest short continuation are distinct. Registration must summarize the active deliverable rather than renaming the task after an unrelated follow-up.
- Runtime busy/idle is not task acceptance. A stopped Agent can leave untested, unreviewed or uninstalled changes. Task data is a persisted display snapshot; unsaved live updates may be absent until the next read. Native paired todo results, not arbitrary tool-shaped JSON, supply the checklist.
- An evidence reference is not independently checked by this UI. Label it honestly.
- The desktop half belongs to the Electron machine. Installing the backend on an SSH server does not install the desktop pane.

## Verification

Verify the directive against the latest visible assistant row using the installed read-only command on the owning connection/profile. Reconcile required remaining items and stage evidence with actual tools. Treat file installation, live command activation and the owner's native window as separate acceptance items.
