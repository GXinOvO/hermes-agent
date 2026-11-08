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

## Protected project volume (r19 candidate, pending independent review)

The additional `protected_owned` provider is discoverable through the same bundled plugin.
R19 qualifies the explicit private-offline project-volume combination through native config,
PluginManager, terminal/file tools and process_manage. The original r17 `/cas` launch permission
failure and r18 shell-bootstrap failure remain historical failures. The protected launch now
enters the payload-owned root after dropping identity and retains only the constant
`LD_LIBRARY_PATH=/opt/runtime/lib` for the protected Python runtime. Existing `owned_process`
acceptance and its configuration above remain separate. This candidate is pending independent
review and has not been installed, enabled or adopted in production.

Candidate configuration (trusted owner input, no default activation):

```yaml
plugins:
  enabled: [owned-process]
  isolation: in_process
  entries:
    owned-process:
      settings:
        protected:
          authority_root: /absolute/authority-package
          state_root: /absolute/existing/private-state
          volume: owner-specific-project-volume
          label: owner-specific-resource-label
          image: sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
terminal:
  backend: protected_owned
  cwd: /cas
```

The selected image must exist locally; constructors use `--pull=never`, private networking and
exact task-labelled IDs. The immutable r16 authority binary/source hashes and system runtime
dependencies are checked before service use. Authority runs as trusted container root in a PID
namespace separate from each payload; payload UID/GID is `127776:456597`, root mode is `0700`.
Native qualification records all payload UID/GID slots, empty supplementary groups, all capability
sets zero, NoNewPrivs, mount/FD layout and paired lawful writes. Signal/ptrace/capability/identity
attacks against live trusted PID1 fail with EPERM; trusted state/helper/control FD reads fail
with EACCES. Absent host proc/Docker sockets are separate visibility observations. The PID1
reaper retains only KILL/SETUID/SETGID (0xe0), drops bounding/inheritable/ambient capabilities,
and leaves SIGINT at default. This assumes the native host controller and configuration owner
are trusted; it does not establish hostile-host or arbitrary hostile-interpreter provenance.

Project data is a Docker volume backed by the authority SQLite database; state-root identity
is the trusted controller owner, not a hostile-payload provenance claim. Existing volume labels
must match; another owner's data is never imported or overwritten. Bootstrap refreshes trusted
code only. Environment cleanup retains that volume; explicit owner-controlled retirement is
separate. There is no automatic host-directory import/export. Initialization and export use
ordinary terminal/file tools on `/cas`; native qualification covers nested creation, consecutive
writes/patches, independent readback and ordinary terminal mutation under the same authority.
Final conditional publication detects a same-file replace or pwrite after host-side checks.
Cleanup preserves data; normal reopen, idle authority SIGKILL/reopen and a journal-observed
pre-COMMIT SIGKILL recover committed bytes. Each authority incarnation rejects prior tickets;
a fresh full read restores lawful publication. Response loss after a real commit is unknown,
with no automatic retry. Owned process results preserve generation/fingerprint/profile ownership
and are readable by a fresh native interpreter without executing the command again.

No host path should be supplied as `cwd` to simulate isolation. Hostnet, business credentials,
shared Python kernels, mmap/symlink/full POSIX, power-loss durability of host storage, ordinary
ext4 last-replace races and whole-Agent/browser/MCP behavior remain outside this candidate's
claims. The task delivery REPORT and receipts define the exact accepted scope and history.
