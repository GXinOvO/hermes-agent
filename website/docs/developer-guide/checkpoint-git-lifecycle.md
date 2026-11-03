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

Hermes never removes a pre-existing or unknown-owner index lock. A pre-existing lock, a hard crash,
SIGKILL, or a Git that cannot run its cleanup handlers can still block later checkpoints.
Do not infer abandonment from age or a single process/fd scan. The fault tests do not
recover production locks, modify real project repositories, or establish a hard deadline
for blocked filesystem I/O. Snapshot scans, staging and shared-store retention costs are
unchanged; no successful-workload speedup is claimed.

## Existing-lock preflight and diagnosis

A snapshot now refuses an already present project index lock before updating project
metadata, counting project files or reseeding the index. The shared-store operation lock
still wraps the call, and Git still arbitrates any lock appearing after that observation.
The return value remains false: no recovery point was created. Restore also refuses a
known lock before inspecting or modifying working files. Safe restore checks the result
of staging current files; a failed `git add` must not use an old index to compute a plan
that could delete agent-created files before a subsequent checkout fails. Deletion of
agent-created files is deferred until the checkout succeeds, so a lock appearing at
checkout cannot cause those deletions before the failure is returned. The internal snapshot
result distinguishes a published snapshot, an already matching saved state, and a failed
save; restore aborts before changing working files when the safety save failed. The
public `ensure_checkpoint` result remains boolean. The Python deletion phase acquires
its own index lock with exclusive creation, including when there are no checkout targets,
and releases only that exact owned lock in cleanup. Unknown locks are never removed.

`hermes checkpoints locks --json` reports lock identity (`device`, `inode`, size and
nanosecond timestamps) and the project mapping when its metadata matches the project
hash. It performs no Git command, project traversal or lock removal. Malformed or
symlink metadata cannot authorize a mapping; missing ownership information stays
`unknown`, with `recovery_allowed: false`. This is a point-in-time observation, not an
exclusive lease or a proof that a lock was abandoned. An offline maintenance boundary
covering all store writers, or trustworthy writer ownership/lifetime evidence bound to
the exact lock generation, is still required before legacy-lock recovery.
