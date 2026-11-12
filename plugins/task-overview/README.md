# Task overview

A task card for one session plus a device-local follow-up panel for manually
tracked matters. The two features keep business acceptance separate from agent
activity and todo state.

## Follow-up panel

The titlebar entry counts only open matters manually recorded in this device's
IndexedDB. Create a matter from the focused conversation, edit its remaining
steps, associate other focused conversations, explicitly accept it with a note,
or reopen it. A session reference preserves the identity fields actually
observed, marked as unverified. This version disables opening sessions from the
panel, including when a store identifier is present: a copied store or replaced
backend can retain the same identifier. Closing a chat tab or the panel does
not mark a matter complete.

The separate session-review view scans at most 12 sources and the first 100
sessions by creation time per source, plus the host's extra pinned rows. It
cannot infer whether those sessions contain unfinished work. It does not cover
all history, and current execution, real wait-for-input requests, automatic
business-task discovery and cross-device synchronization are not implemented.

The desktop uses local browser storage; clearing app data or switching device or
origin may lose access to the records. Isolated Electron testing does not imply
production installation or activation. Safe session navigation requires a
separate end-to-end source-identity contract.

## What it shows

- Original request and current request, kept separately so a short follow-up
  does not replace the original goal.
- The session's real todo list, read through `get_latest_todo_result` (native
  call pairing), not from a tool result that merely looks like a todo payload.
- Remaining work, deferred suggestions and decisions waiting on the user.
- Five acceptance stages: implementation, tests, review, integration,
  activation. A stage without an evidence reference renders as unverified.
- Registration state: `unregistered`, `registered` or `stale`. A later user
  turn or a file/tool edit expires the previous registration.

## Halves

| Half | Lives in | Loaded by |
| --- | --- | --- |
| Agent | `plugin.yaml`, `__init__.py` | the profile's gateway, as a bundled plugin |
| Desktop | `desktop/plugin.js` | the Electron app, lifted from the profile's `plugins/task-overview/desktop/` |

The agent half registers one gateway command, `task-overview-read`. The desktop
half calls it through `command.dispatch` on the exact owning runtime session and
renders a right-side pane with the SDK's native controls.

## Data boundary

`SessionDB(read_only=True)` over the persisted session store, with the compaction,
branch and undo semantics the display already uses. The command returns tool
occurrence metadata only: tool results, model reasoning and tool arguments are
dropped. The payload is explicitly marked `snapshot_scope=persisted_display` and
carries a fetch time, because content written after the read is not included.

Failures are returned as an explicit error envelope instead of a partial
overview; a session that changed mid-read is refused rather than rendered from
mixed generations. Verification evidence is read in that same command using the
session-bound absolute working directory and route key. If that binding is absent
or the directory is unavailable, the ledger stays unavailable instead of falling
back to the process directory. The pane displays the queried directory and ledger
root beside its evidence.

## Registration directive

An assistant message may carry one line, starting at column zero outside code
blocks or quoted/list content:

```
::task{title="..." goal="..." result="..." remaining="..." deferred="..." decision="..." implementation="passed" implementation_evidence="..." tests="passed" tests_evidence="..." ...}
```

Allowed stage values are `passed`, `failed`, `pending` and `not_applicable`;
`passed` and `not_applicable` require a matching `_evidence` attribute. This is
an agent registration, not an independent verification.

## Build and verify

Install the desktop workspace dependencies with the repository's pinned pnpm,
then run these commands from the repository root:

```sh
node plugins/task-overview/build.mjs
node plugins/task-overview/build.mjs --check
node plugins/task-overview/build.mjs --test
bash scripts/run_tests.sh tests/plugins/test_task_overview_plugin.py -j 1
```

The bundle keeps React and the plugin SDK external so the desktop host supplies
them. Registration uses the desktop's pinned Markdown parser dependencies
(`unified`, `remark-parse`, `remark-gfm`, `remark-math`) to exclude code, quotes,
lists, HTML, tables and math; these are bundled, not additional host imports.
Their copyright and license texts are retained in `desktop/THIRD_PARTY_NOTICES.txt`
and embedded in the generated bundle so a standalone copy preserves them.
The test entrypoint bundles the same source with the same dependencies before running Node tests. The checked-in sources and tests make the shipped bundle reproducible.
Backend installation and native desktop activation are separate: the Electron
machine must load `desktop/plugin.js`, and the selected backend must load the
agent half. Merging this directory does not activate an already-running desktop.
