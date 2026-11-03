# Checkpoint Git timeout lifecycle

Checkpoint writes still synchronously stage the working tree, build a tree and publish a
project ref before `ensure_checkpoint` can report a new recovery point. The directory/turn
deduplication, shared-store operation lock, file limits and pruning policy are unchanged.

`tools/checkpoint_git.py` owns the `_run_git` subprocess lifetime. On POSIX, it starts Git
in a private process group. A timeout or an exception while waiting sends TERM to
that tree, then waits for the managed group to exit against a monotonic two-second
grace. A fast-exiting or already-reaped launcher cannot shorten the writer's grace;
only expiry permits the existing force-tree/group cleanup. Output then drains with
a two-second budget. This lets Git run its signal handlers and release its own index lock;
`subprocess.run(timeout=...)` previously killed Git directly and skipped those handlers.
A drain error is logged without replacing the original interrupt or timeout. Binary
NUL-delimited output continues to preserve literal filenames.

The process-group handling includes Git filters and helpers. The real-process regression
uses an isolated project and store, with a one-shot clean-filter socket handshake proving
that `git add` already holds the project index lock. It covers timeout, descendant exit,
an uncooperative launcher, a launcher that exits before Git, active-store contention,
foreign locks, an injected wait interrupt, and byte/executable-bit/symlink restoration.
The wait-interrupt test exercises `_run_git` cleanup; it does not establish that CLI,
Desktop or tool-executor cancellation reaches a synchronous checkpoint wait.

Hermes never deletes index locks in this change. A pre-existing lock, a hard crash,
SIGKILL, or a Git that cannot run its cleanup handlers can still block later checkpoints.
Do not infer abandonment from age or a single process/fd scan. The fault tests do not
recover production locks, modify real project repositories, or establish a hard deadline
for blocked filesystem I/O. Snapshot scans, staging and shared-store retention costs are
unchanged; no successful-workload speedup is claimed.
