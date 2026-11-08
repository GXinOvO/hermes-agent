"""Internal patch snapshots, scoped to one public targeted-edit call.

Validation and apply share immutable authority versions. Only own successful
commits replace them; internal reads never become public full-read grants.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from tools.file_backend_probe import FileBackendUnavailable

_active_patch: ContextVar[Any] = ContextVar('hermes_protected_patch', default=None)


@dataclass
class ProtectedPatch:
    env: Any
    task_id: str
    tickets: dict = field(default_factory=dict)
    full: dict = field(default_factory=dict)
    changed: set = field(default_factory=set)
    parent_proofs: dict = field(default_factory=dict)

    def target(self, path):
        if path not in self.tickets:
            from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock
            ticket = replace(self.env.target(path), task_id=self.task_id)
            with _read_tracker_lock:
                data = _read_tracker.get(self.task_id) or {}
                self.full[path] = (not ticket.absent and
                    data.get('protected_tickets', {}).get(path) == ticket and
                    data.get('full_write_baselines', {}).get(path) == ticket.version)
            self.tickets[path] = ticket
        return self.tickets[path]

    def publish(self, path, content):
        old = self.commit_target(path)
        result = self.env.publish(old, content)
        if not result.exit_code:
            new = result._protected_ticket
            if new is None:
                raise FileBackendUnavailable('protected patch commit identity missing; outcome unknown')
            self.tickets = {p: replace(t, generation=new.generation) for p, t in self.tickets.items()}
            self.tickets[path] = new
            self.full[path] = old.absent or self.full.get(path, False)
            self.changed.add(path)
            self.remember_parents(old, new)
        return result

    def remember_parents(self, before, after):
        if not before.absent:
            return
        from tools.file_protected_fuse import Request
        a, b = Request.from_buffer_copy(before.packet).expected, Request.from_buffer_copy(after.packet).expected
        for depth in range(a.depth + 1, b.depth + 1):
            directory = '/'.join(before.path.split('/')[:depth + 2])
            self.parent_proofs[directory] = after.path

    def commit_target(self, path):
        ticket = self.target(path)
        if ticket.absent:
            from tools.file_protected_fuse import Request
            while True:
                expected = Request.from_buffer_copy(ticket.packet).expected
                parent = '/'.join(path.split('/')[:expected.depth + 3])
                proof_path = self.parent_proofs.get(parent)
                if not proof_path:
                    break
                ticket = self.env.rebase_parents(ticket, self.tickets[proof_path])
                self.tickets[path] = ticket
        return ticket

    def namespace(self, source, destination=None):
        old = self.target(source)
        dest = self.commit_target(destination) if destination else None
        result = self.env.namespace(old, dest)
        if not result.exit_code:
            generation = result._source_ticket.generation
            self.tickets = {p: replace(t, generation=generation) for p, t in self.tickets.items()}
            self.tickets[source] = result._source_ticket
            self.changed.add(source)
            allowed = self.full.pop(source, False)
            if destination:
                self.tickets[destination] = result._protected_ticket
                self.full[destination] = allowed
                self.changed.add(destination)
                self.remember_parents(dest, result._protected_ticket)
        return result

    def record(self):
        from tools.file_tools_read_tracking import _mark_full_write_baseline
        from tools.file_backend_io import checked_generation
        try:
            generation = checked_generation(self.env)
        except FileBackendUnavailable:
            return  # Unknown outcomes cannot grant baselines or mask partial results.
        for path in sorted(self.changed):
            ticket = self.tickets[path]
            if not ticket.absent and self.full.get(path) and ticket.generation == generation:
                _mark_full_write_baseline(path, self.task_id, published_ticket=ticket)


def protected_patch(operations):
    state = _active_patch.get()
    if state is not None and state.env is not operations.env:
        from tools.file_backend_io import revoke_backend_reads
        revoke_backend_reads(state.env)
        revoke_backend_reads(operations.env)
        raise FileBackendUnavailable('protected patch backend changed; refusing mutation')
    return state


@contextmanager
def protected_patch_scope(operations, task_id):
    from tools.file_protected_fuse import protected_fuse
    if protected_fuse(operations.env) is None:
        yield None
        return
    state = ProtectedPatch(operations.env, task_id)
    token = _active_patch.set(state)
    try:
        yield state
    finally:
        _active_patch.reset(token)
