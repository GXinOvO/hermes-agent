"""Single-pass directory scans for checkpoint admission and store accounting."""
from contextlib import closing
import fnmatch
import os
from pathlib import Path
import stat
from typing import Generator


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def iter_entries(
    root: str | Path,
    *,
    exclude_dirs: tuple[str, ...] = (),
    exclude_files: tuple[str, ...] = (),
) -> Generator[os.DirEntry, None, None]:
    """Stream entries without descending through directory symlinks.

    Only one directory is open at a time. Callers close the generator if they
    stop early. An unreadable subtree is skipped independently, as with rglob.

    ``exclude_dirs`` prunes directory names; ``exclude_files`` drops file names
    and, like git's own ignore rules, directory names too. An excluded name is
    neither yielded nor descended, so callers that pass the store's excludes
    never count or size the trees a checkpoint would skip anyway.
    """
    pending = [root]
    while pending:
        try:
            with os.scandir(pending.pop()) as scan:
                for entry in scan:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    name = entry.name
                    if _matches(name, exclude_files):
                        continue
                    if is_dir and _matches(name, exclude_dirs):
                        continue
                    yield entry
                    if is_dir:
                        pending.append(entry.path)
        except OSError:
            continue


def file_count(
    root: str | Path,
    limit: int,
    *,
    exclude_dirs: tuple[str, ...] = (),
    exclude_files: tuple[str, ...] = (),
) -> int:
    """Count files, directories and links, stopping once the limit is exceeded.

    Excluded names are not checkpoint candidates: they neither count nor
    descend, so a project's dependency and build trees cannot push it past
    ``limit`` and silently disable its checkpoints.
    """
    count = 0
    with closing(
        iter_entries(root, exclude_dirs=exclude_dirs, exclude_files=exclude_files)
    ) as entries:
        for _entry in entries:
            count += 1
            if count > limit:
                break
    return count


def dir_size_bytes(root: str | Path) -> int:
    """Sum regular-file sizes, including file-link targets but not directory links."""
    total = 0
    with closing(iter_entries(root)) as entries:
        for entry in entries:
            try:
                metadata = entry.stat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    return total
