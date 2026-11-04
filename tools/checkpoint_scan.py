"""Single-pass directory scans for checkpoint admission and store accounting."""
from contextlib import closing
import os
from pathlib import Path
import stat
from typing import Generator


def iter_entries(root: str | Path) -> Generator[os.DirEntry, None, None]:
    """Stream entries without descending through directory symlinks.

    Only one directory is open at a time. Callers close the generator if they
    stop early. An unreadable subtree is skipped independently, as with rglob.
    """
    pending = [root]
    while pending:
        try:
            with os.scandir(pending.pop()) as scan:
                for entry in scan:
                    yield entry
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def file_count(root: str | Path, limit: int) -> int:
    """Count files, directories and links, stopping once the limit is exceeded."""
    count = 0
    with closing(iter_entries(root)) as entries:
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
