# Owned process terminal backend

Opt-in Linux terminal provider, backend name `owned_process`. It implements the native
`TerminalEnvironmentProvider` ABI and is loaded by the normal plugin discovery path.
The plugin contains every runtime helper; it does not import test fixtures or a kernel attachment.

## Configuration

Place this complete directory under the intended profile's `plugins/owned-process` only after
review and adoption approval. Select it through normal native configuration:

```yaml
plugins:
  enabled: [owned-process]
  isolation: in_process
  entries:
    owned-process:
      settings:
        project_root: /absolute/canonical/project
        state_root: /absolute/canonical/private-state
        network: private
terminal:
  backend: owned_process
  cwd: /workspace
```

The two roots must be disjoint, canonical absolute directories. The project is a writable host
mount at `/workspace`. Per-environment state is mounted at `/state`; the host path of that state,
user home, credentials and control pipes are not exposed to the command. Configure these paths
as trusted owner input, not from command output. The plugin creates the state root if necessary.
Both writable roots must also be disjoint from the installed runtime: containing/equal roots,
directory bind aliases and hardlinks to runtime files are rejected before payload creation.
Admission scans directory/file device+inode identities without following project symlinks;
symlinks resolve within the restricted payload mount namespace. Payloads cannot mount new
aliases or create cross-mount hardlinks to `/boundary`. This assumes the same-UID host owner
is trusted and does not concurrently remount/edit configuration or runtime during admission;
it is not protection against a hostile host owner or arbitrary host filesystem escape.
`hostnet` is the explicit alternative to `private`; it shares the host network namespace.
Neither mode declares a universal network or filesystem security guarantee.

Linux, `/usr/bin/bwrap`, `/usr/bin/unshare`, `/usr/bin/python3`, Bash, and working unprivileged
user/mount/PID namespaces are required. The plugin fails closed when creation or a trusted
termination receipt cannot be verified. There is no fallback to local execution. It retains
`skip_container_guards=False`: ordinary dangerous-command approvals still apply to the mounted
host project. Unknown factory kwargs are accepted for ABI compatibility. Stream placement and
PTY interaction are not advertised.

## Runtime contract

Each command has a unique generation and a dedicated PID-namespace init. The trusted init reaps
the root and all descendants; command lifetime cancels remaining descendants on root exit,
while background lifetime retains them until they exit or the owner cancels. `setsid` and
forked descendants stay in the same PID namespace. The private cancel pipe, retained launcher
handle/start ticks and `waitpid` ECHILD receipt bind termination to the exact invocation.
The command gets no control/report FDs. Cleanup cancels only this environment's invocations.

A sole reader demultiplexes events into per-call bounded spools (8 MiB unread bytes, 32 handles).
A per-call pump may wait for its consumer without parking that event reader. Exceeding the spool
bound cancels the invocation with exit 122 and an explicit `output_backpressure_limit`
diagnostic. Actively consumed streams are not silently truncated by this transport. The core
process result keeps its existing bounded/redacted tail and retention policy.

Core integration makes the output reader the only final-completion owner. Verified termination
can precede output drain: kill/poll/log/list/wait then report `process_running=false` and
`output_pending=true`, do not consume completion, and do not promise another process exit.
The reader later persists the final result and queues at most one completion. Durable receipts
retain the exact owned generation/lifecycle under the existing result fingerprint. Pre-existing
v1 receipts remain compatible. Fresh process queries verify profile and session lineage and do
not execute the command again.

BaseEnvironment shell export and cwd snapshots are supported. This backend does not qualify a
shared persistent Python kernel, arbitrary live-object identity, the parent F02/A3 isolation
contract, FUSE/ext4 publication, browser/Desktop placement or production adoption. Those remain
separate acceptance gates.
