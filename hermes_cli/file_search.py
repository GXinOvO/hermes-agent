"""Bounded ripgrep execution for the Files pane (streamed, reaped on cancellation)."""
import asyncio
import base64
import fnmatch
import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

import anyio

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hermes_cli._subprocess_compat import windows_hide_flags


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    root: str = Field(min_length=1, max_length=32768)
    query: str = Field(min_length=1, max_length=4096)
    mode: Literal["name", "content"] = "content"
    scope: str = Field(default="", max_length=4096)
    regex: bool = False
    caseSensitive: bool = False
    showIgnored: bool = False
    maxResults: int = Field(default=1000, ge=1, le=2000)
    timeoutMs: int = Field(default=15000, ge=50, le=30000)

    @field_validator("root", "query", "scope")
    @classmethod
    def no_control_bytes(cls, value):
        if any(c in value for c in ("\0", "\r", "\n")):
            raise ValueError("Search fields must not contain NUL or newline")
        return value


def resolve_rg() -> str | None:
    """Read the managed executable first; search platform paths without installing."""
    import pm
    from hermes_platform.resolver import locate_command

    package = pm.installed_package("ripgrep")
    if package and package.binary:
        return str(package.binary)
    command = locate_command("rg").command
    return command[0] if command else None


# Keep the Files tree's reveal floor: these four names may be shown explicitly.
_ALWAYS_HIDDEN = frozenset({
    ".git", ".hg", ".svn", "node_modules", "bower_components", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle", ".idea",
    "dist", "build", "out", "target", "vendor", "Pods", ".next", ".nuxt", ".svelte-kit",
    ".output", ".turbo", ".parcel-cache", ".cache", ".terraform", ".expo", ".angular",
    "coverage", ".DS_Store", "Thumbs.db",
})
_REVEALABLE = frozenset({"env", "out", "vendor", "coverage"})
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", "mcp-tokens", "pairing"})


def _hidden_names(request: SearchRequest) -> set[str]:
    return {name.lower() for name in _ALWAYS_HIDDEN - (_REVEALABLE if request.showIgnored else set())}


def _private_files() -> set[Path]:
    from hermes_constants import get_default_hermes_root, get_hermes_home

    roots = {get_hermes_home().resolve(), get_default_hermes_root().resolve()}
    return {(home / "config.yaml").resolve() for home in roots} | {
        (child / "config.yaml").resolve() for home in roots if (home / "profiles").is_dir()
        for child in (home / "profiles").iterdir() if not child.name.startswith(".") and child.is_dir()
    }


def _blocked_path(path: Path, request: SearchRequest) -> bool:
    from hermes_cli.web_routers.files import _SENSITIVE_MANAGED_FILE_BASENAMES

    parts = {part.lower() for part in path.parts}
    sensitive_names = (_SENSITIVE_MANAGED_FILE_BASENAMES - {"config.yaml"}) | {".env", ".envrc"}
    return (bool(parts & (_SECRET_DIRS | _hidden_names(request) | sensitive_names))
            or any(part.startswith(".env.") for part in parts)
            or path in _private_files())


def validate_search_root(request: SearchRequest) -> tuple[Path, str, str]:
    """Validate exact scopes before interpreting glob syntax; never follow scope links."""
    raw = request.root
    if os.name != "nt" and (re.match(r"^[a-zA-Z]:[\\/]", raw) or raw.startswith("\\\\")):
        raise ValueError("Search root belongs to another filesystem")
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Search root is not a directory")
    # POSIX backslashes are legal filename characters, not path separators.
    scope = request.scope.replace("\\", "/") if os.name == "nt" else request.scope
    while scope.startswith("./"):
        scope = scope[2:]
    if scope.startswith(("/", "!")) or re.match(r"^[a-zA-Z]:", scope) or ".." in scope.split("/"):
        raise ValueError("Search scope must stay inside the selected root")
    if not scope or scope == ".":
        return root, ".", ""
    candidate = root / scope
    existing = candidate.exists() or candidate.is_symlink()
    parts = scope.rstrip("/").split("/")
    index = len(parts) if existing else next(
        (i for i, part in enumerate(parts) if re.search(r"[*?\[{}]", part)), len(parts))
    current = root
    for part in parts[:index]:
        current /= part
        if current.is_symlink():
            raise ValueError("Search scope must not traverse a symbolic link")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Search scope resolves outside the selected root")
    if index == len(parts):
        if not resolved.is_dir() and not resolved.is_file():
            raise ValueError("Search scope must be a regular file or directory")
        return root, resolved.relative_to(root).as_posix() or ".", ""
    _glob_variants(scope)
    return root, "/".join(parts[:index]) or ".", scope


@lru_cache(maxsize=128)
def _glob_variants(pattern: str) -> tuple[str, ...]:
    pending, complete = [pattern], []
    while pending:
        value = pending.pop()
        opening = value.rfind("{")
        closing = value.find("}", opening)
        if opening < 0 or closing < 0:
            complete.append(value)
            continue
        alternatives = value[opening + 1:closing].split(",")
        if len(alternatives) == 1:
            complete.append(value)
            continue
        if len(pending) + len(complete) + len(alternatives) > 128:
            raise ValueError("Search scope has too many brace alternatives")
        pending.extend(value[:opening] + part + value[closing + 1:] for part in alternatives)
    return tuple(complete)


def _matches_glob(relative: str, pattern: str) -> bool:
    return any(_matches_glob_variant(relative, variant) for variant in _glob_variants(pattern))


def _matches_glob_variant(relative: str, pattern: str) -> bool:
    # A set of glob-segment states avoids recursive ** backtracking. fnmatch's
    # generated segment regex is bounded and never evaluates the user's query.
    segments = (["**"] if "/" not in pattern else []) + pattern.split("/")
    states = {0}

    def expand():
        for index in range(len(segments)):
            if index in states and segments[index] == "**":
                states.add(index + 1)

    expand()
    for part in relative.split("/"):
        states = {index if segments[index] == "**" else index + 1
                  for index in states if index < len(segments)
                  and (segments[index] == "**" or fnmatch.fnmatchcase(part, segments[index]))}
        expand()
    return len(segments) in states


def _accepts_path(root: Path, rel: str, target: str, glob: str, request: SearchRequest) -> bool:
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Search engine returned a path outside its root")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return False
    if _blocked_path(root, request) or _blocked_path(root / relative, request):
        return False
    if glob:
        return _matches_glob(rel, glob)
    return target == "." or rel == target or rel.startswith(target.rstrip("/") + "/")


def _escape_glob(value: str) -> str:
    return "".join("\\" + char if char in "\\*?[]{}!" else char for char in value)


def _pruning_globs(root: Path, target: str) -> list[str]:
    """Negative siblings prune a static scope prefix without overriding ignores."""
    parent, relative = root, ""
    patterns, size = [], 0
    for component in Path(target).parts if target != "." else ():
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == component:
                    continue
                pattern = f"!/{relative}{_escape_glob(entry.name)}"
                size += len(pattern.encode("utf-8")) + 4
                if size > 12000:
                    raise ValueError("Search scope has too many siblings to prune safely")
                patterns.extend(["--glob", pattern])
        parent /= component
        relative += _escape_glob(component) + "/"
    return patterns


def search_arguments(request: SearchRequest, target: str, glob: str,
                     root: Path | None = None, private_files: set[Path] | None = None) -> list[str]:
    from hermes_cli.web_routers.files import _SENSITIVE_MANAGED_FILE_BASENAMES

    args = ["--no-config", "--hidden", "--no-follow", "--no-require-git", "--color=never", "--threads=1"]
    if root is not None:
        args += _pruning_globs(root, target)
        for private in private_files if private_files is not None else _private_files():
            if private.is_relative_to(root):
                args += ["--iglob", "!/" + _escape_glob(private.relative_to(root).as_posix())]
    if request.showIgnored:
        args.append("--no-ignore")
    for name in sorted(_hidden_names(request) | _SECRET_DIRS):
        args.extend(["--iglob", f"!{name}"])
    for name in sorted((_SENSITIVE_MANAGED_FILE_BASENAMES - {"config.yaml"}) | {".env", ".env.*", ".envrc"}):
        args.extend(["--iglob", f"!{name}"])
    # Positive globs and explicit file operands override .gitignore. Always walk
    # from the selected root and apply scope only after rg's ignore decisions.
    if request.mode == "name":
        return args + ["--files", "--null", "--", "."]
    args += ["--json", "--line-buffered", "--crlf"]
    args.append("--case-sensitive" if request.caseSensitive else "--ignore-case")
    if not request.regex:
        args.append("--fixed-strings")
    return args + ["-e", request.query, "--", "."]


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _text(field: dict) -> str:
    if "text" in field:
        return field["text"]
    return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")


def _snippet(line: str, spans: list[tuple[int, int]]) -> tuple[str, list[dict]]:
    # Python indices here have already been translated from rg's byte offsets.
    # Clip even a single regex match spanning the whole line to a bounded view.
    first = spans[0][0] if spans else 0
    start = max(0, first - 100)
    end = min(len(line), first + 300)
    prefix = "…" if start else ""
    visible = prefix + line[start:end] + ("…" if end < len(line) else "")
    converted = [{"from": _utf16(prefix + line[start:a]), "to": _utf16(prefix + line[start:min(b, end)])}
                 for a, b in spans if start <= a < end]
    return visible, converted


def parse_rg_match(record: dict, root: Path, display_root: str) -> list[dict]:
    if record.get("type") != "match":
        return []
    data = record["data"]
    rel = _text(data["path"]).removeprefix("./")
    if os.name == "nt":
        rel = rel.replace("\\", "/")
    path = root / rel
    if not path.resolve().is_relative_to(root):
        raise ValueError("Search engine returned a path outside its root")
    raw_line = (data["lines"]["text"].encode("utf-8") if "text" in data["lines"]
                else base64.b64decode(data["lines"]["bytes"])).rstrip(b"\r\n")
    line = raw_line.decode("utf-8", errors="replace")
    submatches = data.get("submatches", [])
    pairs = [(s["start"], s["end"]) for s in submatches]
    if not pairs:
        return []
    first = pairs[0][0]
    first_index = len(raw_line[:first].decode("utf-8", errors="replace"))
    # rg sorts non-overlapping submatches. Translate only the visible window;
    # decoding every prefix of a line with thousands of hits is quadratic.
    spans = []
    for a, b in pairs:
        if a - first > 1200:  # at most 300 visible four-byte codepoints
            break
        index = len(raw_line[:a].decode("utf-8", errors="replace"))
        if index >= first_index + 300:
            break
        end = min(b, a + 1200)
        spans.append((index, len(raw_line[:end].decode("utf-8", errors="replace"))))
    text, ranges = _snippet(line, spans)
    return [{"path": str(Path(display_root) / rel), "rel": rel, "line": data["line_number"],
             "col": _utf16(raw_line[:first].decode("utf-8", errors="replace")),
             "text": text, "ranges": ranges}]


class _OutputLimit(Exception):
    pass


async def _records(reader, separator):
    # readuntil has a fixed ceiling; readline converts LimitOverrunError to
    # ValueError and loses the distinction between bad output and a huge line.
    while True:
        try:
            record = await reader.readuntil(separator)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise ValueError("ripgrep emitted an incomplete record") from exc
            return
        except asyncio.LimitOverrunError as exc:
            raise _OutputLimit("A ripgrep output record exceeded 2 MiB; results are partial") from exc
        yield record[:-1]


async def _read_stderr(reader):
    retained = bytearray()
    while chunk := await reader.read(4096):
        retained.extend(chunk[:max(0, 16384 - len(retained))])
    return bytes(retained)


def _query_arguments(request):
    return ["--case-sensitive" if request.caseSensitive else "--ignore-case",
            *([] if request.regex else ["--fixed-strings"]), "-e", request.query]


class _SearchRun:
    """A producer owns processes independently of the HTTP consumer's next yield."""

    def __init__(self, request, binary, root, target, glob, queue):
        self.request, self.binary, self.root = request, binary, root
        self.target, self.glob, self.queue = target, glob, queue
        self.processes = []
        self.stderr_tasks = []
        self.feeder = None
        self.count = 0
        self.reason = None
        self.failure = None
        self.warnings = []

    async def launch(self, args, *, stdin=None):
        process = await asyncio.create_subprocess_exec(
            self.binary, *args, cwd=self.root, stdin=stdin or asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024, creationflags=windows_hide_flags(),
        )
        self.processes.append(process)
        self.stderr_tasks.append(asyncio.create_task(_read_stderr(process.stderr)))
        return process

    async def feed_names(self, walker, filter_process):
        try:
            async for raw in _records(walker.stdout, b"\0"):
                rel = raw.decode("utf-8", errors="strict").removeprefix("./")
                if _accepts_path(self.root, rel, self.target, self.glob, self.request):
                    filter_process.stdin.write(raw + b"\0")
                    await filter_process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # An invalid filter regex exits before consuming the walker. Its
            # stderr/exit below is authoritative, not this expected EPIPE.
            if walker.returncode is None:
                walker.kill()
        finally:
            filter_process.stdin.close()

    async def stream(self):
        request = self.request
        if request.mode == "name":
            engine = await self.launch(
                ["--no-config", "--null-data", "--line-buffered", "--color=never", "--no-filename",
                 "--no-line-number", "--no-heading", *_query_arguments(request), "--", "-"],
                stdin=asyncio.subprocess.PIPE,
            )
            walker = await self.launch(search_arguments(request, self.target, self.glob, self.root))
            self.feeder = asyncio.create_task(self.feed_names(walker, engine))
        else:
            engine = await self.launch(search_arguments(request, self.target, self.glob, self.root))
        async for raw in _records(engine.stdout, b"\0" if request.mode == "name" else b"\n"):
            if request.mode == "name":
                rel = raw.decode("utf-8", errors="strict").removeprefix("./")
                hits = [{"path": str(Path(request.root) / rel), "rel": rel}]
            else:
                hits = parse_rg_match(json.loads(raw), self.root, request.root)
            hits = [hit for hit in hits if _accepts_path(self.root, hit["rel"], self.target, self.glob, request)]
            if hits:
                self.count += len(hits)
                await self.queue.put({"type": "batch", "hits": hits})
                if self.count >= request.maxResults:
                    self.reason = "result_limit"
                    return
        if self.feeder:
            await self.feeder
        await asyncio.gather(*(process.wait() for process in self.processes))
        await asyncio.gather(*self.stderr_tasks)

    async def cleanup(self):
        if self.feeder:
            self.feeder.cancel()
            await asyncio.gather(self.feeder, return_exceptions=True)
        for process in self.processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # the OS reaped it between the check and the signal
        # Drain stdout BEFORE wait(): a killed process with a full StreamReader
        # otherwise waits forever for its pipe transport to close.
        await asyncio.gather(*(self._drain(process.stdout) for process in self.processes))
        await asyncio.gather(*(process.wait() for process in self.processes))
        await asyncio.gather(*self.stderr_tasks)

    @staticmethod
    async def _drain(reader):
        while await reader.read(65536):
            pass

    def terminal(self, started):
        stderr = b"\n".join(task.result() for task in self.stderr_tasks).decode("utf-8", errors="replace").strip()
        warnings = self.warnings + ([stderr[:1000]] if stderr else [])
        if self.failure:
            return {"type": "error", **self.failure}
        if not self.reason and any(p.returncode not in (0, 1) for p in self.processes):
            if "regex parse error" in stderr or "error parsing regex" in stderr:
                return {"type": "error", "code": "invalid_pattern", "message": stderr[:1000]}
            if not self.count:
                return {"type": "error", "code": "search_failed", "message": stderr[:1000] or "ripgrep failed"}
            self.reason = "partial_error"
        return {"type": "done", "truncated": bool(self.reason),
                **({"reason": self.reason} if self.reason else {}), "warnings": warnings,
                "elapsedMs": round((time.monotonic() - started) * 1000)}


async def _finish_run(run, stream_task, cancel_task):
    stream_task.cancel()
    if cancel_task:
        cancel_task.cancel()
    await asyncio.gather(stream_task, *([cancel_task] if cancel_task else []), return_exceptions=True)
    await run.cleanup()


async def _produce(run, started, cancelled):
    stream_task = asyncio.create_task(run.stream())
    cancel_task = asyncio.create_task(cancelled.wait()) if cancelled else None
    try:
        timeout = max(0, run.request.timeoutMs / 1000 - (time.monotonic() - started))
        ready, _ = await asyncio.wait([stream_task, *([cancel_task] if cancel_task else [])], timeout=timeout,
                                      return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in ready:
            run.reason = "cancelled"
        elif stream_task not in ready:
            run.reason = "timeout"
        else:
            await stream_task
    except _OutputLimit as exc:
        run.reason = "output_limit"
        run.warnings.append(str(exc))
    except NotImplementedError:
        run.failure = {"code": "unsupported_runtime", "message":
                       "This file host's event loop cannot start ripgrep; use native Desktop file search or a supported backend"}
    except OSError as exc:
        run.failure = {"code": "rg_unavailable", "message": str(exc)}
    except (ValueError, KeyError, TypeError) as exc:
        run.failure = {"code": "engine_output", "message": str(exc)}
    finally:
        with anyio.CancelScope(shield=True):
            cleanup = asyncio.create_task(_finish_run(run, stream_task, cancel_task))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
    return run.terminal(started)


async def search_files(request: SearchRequest, *, rg_path: str | None = None,
                       cancelled: asyncio.Event | None = None,
                       root_override: Path | None = None) -> AsyncGenerator[dict[str, Any], None]:
    """Yield bounded frames. Closing/cancelling the iterator joins all owned children."""
    started = time.monotonic()
    if cancelled and cancelled.is_set():
        yield {"type": "done", "truncated": True, "reason": "cancelled", "warnings": [], "elapsedMs": 0}
        return
    try:
        root, target, glob = validate_search_root(request.model_copy(update={"root": str(root_override)})
                                                  if root_override else request)
    except (ValueError, OSError, RuntimeError) as exc:
        yield {"type": "error", "code": "invalid_root", "message": str(exc)}
        return
    if _blocked_path(root, request):
        yield {"type": "done", "truncated": False, "warnings": [], "elapsedMs": 0}
        return
    binary = rg_path if rg_path is not None else resolve_rg()
    if not binary:
        yield {"type": "error", "code": "rg_unavailable", "message": "ripgrep is unavailable on the file host"}
        return
    queue = asyncio.Queue(maxsize=8)
    run = _SearchRun(request, binary, root, target, glob, queue)
    producer = asyncio.create_task(_produce(run, started, cancelled))
    read = None
    try:
        while True:
            read = asyncio.create_task(queue.get())
            ready, _ = await asyncio.wait([read, producer], return_when=asyncio.FIRST_COMPLETED)
            if read in ready:
                yield read.result()
            elif producer.done():
                read.cancel()
                await asyncio.gather(read, return_exceptions=True)
                break
        yield producer.result()
    finally:
        with anyio.CancelScope(shield=True):
            if read is not None:
                read.cancel()
            producer.cancel()
            await asyncio.gather(producer, *([read] if read else []), return_exceptions=True)
