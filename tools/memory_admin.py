from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import stat

from tools.memory_tool_store import MemoryStore


def _regular_targets(directory):
    result = {}
    for name in ('MEMORY.md', 'USER.md'):
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError('memory_audit_admin_non_regular_target')
        result[name] = (info.st_dev, info.st_ino)
    return result


@contextmanager
def memory_file_writer(*paths):
    from tools.memory_tool import current_memory_audit_policy

    destinations = {Path(os.path.abspath(path)): Path(path).resolve() for path in paths}
    for path in set(destinations) | set(destinations.values()):
        if ((path.parent.name == 'memories' and path.name in ('MEMORY.md.lock', 'USER.md.lock'))
                or (path.parent.name == 'cron' and path.name == '.memory-audit.lock.lock')):
            raise OSError('memory_audit_admin_lock_file')
    homes = set()
    for path, destination in destinations.items():
        if path.name in ('MEMORY.md', 'USER.md') and path.parent.name == 'memories':
            home = path.parent.parent.resolve()
            if destination != home / 'memories' / path.name:
                raise OSError('memory_audit_admin_non_regular_target')
            homes.add(home)
        elif destination.name in ('MEMORY.md', 'USER.md') and destination.parent.name == 'memories':
            raise OSError('memory_audit_admin_alias')
    homes = sorted(homes)
    if not homes:
        yield False
        return
    if current_memory_audit_policy() is not None:
        raise OSError('memory_audit_admin_forbidden')
    with ExitStack() as locks:
        for home in homes:
            try:
                locks.enter_context(MemoryStore._file_lock(home / 'cron' / '.memory-audit.lock', blocking=False))
                for name in ('MEMORY.md', 'USER.md'):
                    locks.enter_context(MemoryStore._file_lock(home / 'memories' / name, blocking=False))
            except BlockingIOError as error:
                raise OSError('memory_audit_admin_busy') from error
            directory = home / 'memories'
            if directory.is_symlink():
                raise OSError('memory_audit_admin_non_regular_target')
            _regular_targets(directory)
        yield True
        for home in homes:
            _regular_targets(home / 'memories')


def reset_memory_files(directory, names):
    directory = Path(directory)
    with memory_file_writer(*(directory / name for name in names)):
        selected = _regular_targets(directory)
        deleted = []
        for name in names:
            if name not in selected:
                continue
            path = directory / name
            info = path.lstat()
            if (info.st_dev, info.st_ino) != selected[name]:
                raise OSError('memory_audit_admin_target_changed')
            path.unlink()
            if os.path.lexists(path):
                raise OSError('memory_audit_admin_reset_readback_failed')
            deleted.append(name)
        return deleted
