"""Descriptor-relative commits shared by native I/O and isolated backend Python.

Existing-file replacement requires cooperating writers (the caller's storage-key
lock) or OS permissions that exclude foreign mutation during commit. POSIX has no
compare-inode-and-rename operation: this is not a CAS against arbitrary writers.
The descriptor anchor prevents alias redirection; link() publishes absent targets
without overwriting a file that appeared after validation.

Keep this module stdlib-only: its source also executes in remote environments.
"""
import contextlib
import errno
import hashlib
import os
import secrets
import stat

_VERSION_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')


def _version(value):
    return [getattr(value, field) for field in _VERSION_FIELDS]


def _directory_identity(value):
    return [value.st_dev, value.st_ino, value.st_mode]


def _parent_chain(canonical):
    result = []
    path = os.path.dirname(canonical)
    while True:
        try:
            value = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(value.st_mode):
                raise OSError(errno.EAGAIN, 'write ancestor changed', path)
            result.append([path, _directory_identity(value)])
        if path == '/':
            return list(reversed(result))
        path = os.path.dirname(path)


def _file_snapshot(path, *, dir_fd=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, 'write target is not a regular file', path)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
        end = os.fstat(stream.fileno())
        after = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if _version(before) != _version(end) or _version(before) != _version(after):
            raise OSError(errno.EAGAIN, 'write target changed while hashing', path)
        return _version(before), digest.hexdigest()


def _alias_chain(path):
    """Retain symlink identity even when a replacement names the same target."""
    pending = path.split('/')[1:]
    current = '/'
    links = []
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            current = os.path.dirname(current)
            continue
        current = os.path.join(current, part)
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode):
            if len(links) >= 40:
                raise OSError(errno.ELOOP, 'too many write aliases', path)
            link = os.readlink(current)
            links.append([current, _version(value), link])
            current = '/' if os.path.isabs(link) else os.path.dirname(current)
            pending = link.split('/') + pending
    return links


def target_snapshot(path):
    """Snapshot the canonical destination, parent inodes and full byte version."""
    if not os.path.isabs(path) or os.name != 'posix':
        raise OSError(errno.ENOTSUP, 'bound write requires an absolute POSIX path', path)
    canonical = os.path.realpath(path)
    aliases = _alias_chain(path)
    parents = _parent_chain(canonical)
    version, digest = _file_snapshot(canonical)
    if os.path.realpath(path) != canonical or _parent_chain(canonical) != parents or _alias_chain(path) != aliases:
        raise OSError(errno.EAGAIN, 'write target changed during binding', path)
    return {'canonical': canonical, 'parents': parents, 'aliases': aliases, 'version': version, 'sha256': digest}


def _open_parent(target):
    """Walk the canonical path without following replaced ancestors; retain its FD."""
    expected = dict(target['parents'])
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if expected.get('/') != _directory_identity(os.fstat(fd)):
            raise OSError(errno.EAGAIN, 'write root changed')
        path = ''
        for part in os.path.dirname(target['canonical']).split('/')[1:]:
            if not part:
                continue
            path += '/' + part
            if path not in expected:
                # A concurrent mkdir is accepted as a directory, never as a link.
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if path in expected and expected[path] != _directory_identity(os.fstat(child)):
                    raise OSError(errno.EAGAIN, 'write ancestor changed', path)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate_target(path, target, parent_fd, name):
    if os.path.realpath(path) != target['canonical'] or _alias_chain(path) != target['aliases']:
        raise OSError(errno.EAGAIN, 'write target alias changed', path)
    for ancestor, identity in target['parents']:
        if _directory_identity(os.lstat(ancestor)) != identity:
            raise OSError(errno.EAGAIN, 'write ancestor changed', ancestor)
    current_parent = os.stat(os.path.dirname(target['canonical']), follow_symlinks=False)
    if _directory_identity(current_parent) != _directory_identity(os.fstat(parent_fd)):
        raise OSError(errno.EAGAIN, 'write parent changed', path)
    version, digest = _file_snapshot(name, dir_fd=parent_fd)
    if version != target['version'] or digest != target['sha256']:
        raise OSError(errno.EAGAIN, 'write target version changed', path)


def commit_write(path, data, target):
    """Commit to the bound parent/name, refusing drift before replacing any target.

    No caller may fall back to an unbound write when this returns an error.
    """
    parent_fd = None
    tmp = None
    try:
        if os.path.realpath(path) != target['canonical']:
            raise OSError(errno.EAGAIN, 'write target alias changed', path)
        parent_fd = _open_parent(target)
        name = os.path.basename(target['canonical'])
        _validate_target(path, target, parent_fd, name)
        mode = None if target['version'] is None else stat.S_IMODE(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode)
        candidate = '.hermes-tmp.' + secrets.token_hex(12)
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o666 if mode is None else 0o600, dir_fd=parent_fd)
        tmp = candidate
        with os.fdopen(fd, 'wb') as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
        _validate_target(path, target, parent_fd, name)
        if target['version'] is None:
            # Atomic no-replace even when a foreign file arrives after validation.
            os.link(tmp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        else:
            os.replace(tmp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        return None
    except OSError as exc:
        return f'Bound write refused: {exc.strerror or exc}: {exc.filename or path}'
    finally:
        if parent_fd is not None:
            if tmp is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=parent_fd)
            os.close(parent_fd)
