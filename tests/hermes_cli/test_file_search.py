"""Real-rg contract for the Files pane: one bounded process, no renderer file reads."""
import asyncio
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "project"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "nested" / "示例.py").write_bytes("标题😀 TARGET_FIELD\r\nTARGET_FIELD = 2\r\n".encode())
    (root / "src" / "app.ts").write_text("TARGET_FIELD = 3\n")
    (root / "docs" / "note.md").write_text("TARGET_FIELD in docs\n")
    (root / ".env").write_text("TARGET_FIELD secret\n")
    (root / "auth.json").write_text('"TARGET_FIELD secret"')
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.js").write_text("TARGET_FIELD dependency\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("TARGET_FIELD ignored\n")
    return root


def test_content_search_reports_utf16_columns_and_respects_scope(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="TARGET_FIELD", mode="content", scope="src/**/*.py"),
            rg_path=shutil.which("rg"),
        )]

    frames = asyncio.run(run())
    hits = [hit for frame in frames if frame["type"] == "batch" for hit in frame["hits"]]
    assert [(hit["rel"], hit["line"]) for hit in hits] == [("src/nested/示例.py", 1), ("src/nested/示例.py", 2)]
    assert hits[0]["col"] == 5  # two CJK chars + surrogate pair + one space
    assert hits[0]["ranges"][0] == {"from": 5, "to": 17}
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]


def test_basename_glob_scope_matches_files_at_any_depth(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="TARGET_FIELD", mode="content", scope="*.py"),
            rg_path=shutil.which("rg"),
        )]

    frames = asyncio.run(run())
    assert {hit["rel"] for frame in frames if frame["type"] == "batch" for hit in frame["hits"]} == {"src/nested/示例.py"}


def test_unsupported_subprocess_loop_returns_explicit_capability_error(tree, monkeypatch):
    from hermes_cli.file_search import SearchRequest, search_files

    async def unavailable(*args, **kwargs):
        raise NotImplementedError("Selector loop has no subprocess transport")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="TARGET_FIELD", mode="content"), rg_path=shutil.which("rg"),
        )]

    frames = asyncio.run(run())
    assert frames[-1]["type"] == "error" and frames[-1]["code"] == "unsupported_runtime"


@pytest.mark.parametrize("secret", [".env", ".ENV.local", "auth.json", "credentials"])
@pytest.mark.parametrize("show_ignored", [False, True])
def test_sensitive_ancestor_cannot_become_the_search_root(tree, secret, show_ignored):
    nested = tree / secret / "nested"
    if (tree / secret).is_file():
        (tree / secret).unlink()
    nested.mkdir(parents=True)
    (nested / "key.txt").write_text("TARGET_FIELD private\n")
    frames = collect(nested, showIgnored=show_ignored)
    assert not hits(frames)
    assert frames[-1]["type"] == "done"
    assert not hits(collect(tree, scope=f"{secret}/nested", showIgnored=show_ignored))


def test_no_match_is_distinct_from_invalid_regex(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run(query, regex=False):
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query=query, mode="content", regex=regex),
            rg_path=shutil.which("rg"),
        )]

    empty = asyncio.run(run("NOT_HERE"))
    assert empty[-1]["type"] == "done" and not empty[-1]["truncated"]
    assert not [frame for frame in empty if frame["type"] == "batch"]
    bad = asyncio.run(run("([", True))
    assert bad[-1]["type"] == "error" and bad[-1]["code"] == "invalid_pattern"


def test_secrets_and_excluded_directories_never_appear_even_with_include_glob(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="TARGET_FIELD", mode="content", scope="**/*", showIgnored=True),
            rg_path=shutil.which("rg"),
        )]

    frames = asyncio.run(run())
    rels = {hit["rel"] for frame in frames if frame["type"] == "batch" for hit in frame["hits"]}
    assert "ignored.txt" in rels
    assert ".env" not in rels and "auth.json" not in rels
    assert "node_modules/pkg.js" not in rels


def test_exact_file_scope_survives_many_root_siblings(tmp_path):
    root = tmp_path / "large-project"
    root.mkdir()
    target = root / "target.py"
    target.write_text("TARGET_SCOPE_REPRO\n")
    (root / "other.py").write_text("TARGET_SCOPE_REPRO outside scope\n")
    target_dir = root / "target-dir"
    target_dir.mkdir()
    (target_dir / "nested.py").write_text("TARGET_SCOPE_REPRO nested\n")
    for index in range(1600):
        (root / f"sibling-directory-{index:04d}-long-name").mkdir()

    frames = collect(root, query="TARGET_SCOPE_REPRO", scope="target.py")
    directory_frames = collect(root, query="TARGET_SCOPE_REPRO", scope="target-dir")

    assert [(hit["rel"], hit["line"]) for hit in hits(frames)] == [("target.py", 1)]
    assert [hit["rel"] for hit in hits(directory_frames)] == ["target-dir/nested.py"]
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]
    assert directory_frames[-1]["type"] == "done" and not directory_frames[-1]["truncated"]


def test_scope_cannot_escape_selected_root(tree):
    from hermes_cli.file_search import SearchRequest, validate_search_root

    with pytest.raises(ValueError, match="scope"):
        validate_search_root(SearchRequest(root=str(tree), query="x", mode="content", scope="../outside"))


def test_dash_leading_literal_is_not_an_engine_option(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    (tree / "dash.txt").write_text("--help is a field\n")

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="--help", mode="content"), rg_path=shutil.which("rg"),
        )]

    frames = asyncio.run(run())
    assert any(hit["rel"] == "dash.txt" for f in frames if f["type"] == "batch" for hit in f["hits"])


def test_cancellation_stops_and_reaps_process(tree):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run():
        cancelled = asyncio.Event()
        cancelled.set()
        return [frame async for frame in search_files(
            SearchRequest(root=str(tree), query="TARGET_FIELD", mode="content"),
            rg_path=shutil.which("rg"), cancelled=cancelled,
        )]

    frames = asyncio.run(run())
    assert frames[-1]["type"] == "done" and frames[-1]["reason"] == "cancelled"


def collect(root, query="TARGET_FIELD", **kwargs):
    from hermes_cli.file_search import SearchRequest, search_files

    binary = shutil.which("rg")
    assert binary, "Real ripgrep is required for this contract"

    async def run():
        return [frame async for frame in search_files(
            SearchRequest(root=str(root), query=query, **kwargs), rg_path=binary,
        )]

    return asyncio.run(run())


def hits(frames):
    return [hit for frame in frames if frame["type"] == "batch" for hit in frame["hits"]]


@pytest.mark.parametrize("scope", ["**/*", "ignored.txt", "ignored-dir", "ignored-dir/**/*.py"])
def test_scope_never_overrides_gitignore(tree, scope):
    (tree / ".gitignore").write_text("ignored.txt\nignored-dir/\n")
    (tree / "ignored-dir").mkdir()
    (tree / "ignored-dir" / "secret.py").write_text("TARGET_FIELD\n")
    hidden = collect(tree, scope=scope)
    assert not any(hit["rel"].startswith("ignored") for hit in hits(hidden))
    revealed = collect(tree, scope=scope, showIgnored=True)
    assert any(hit["rel"].startswith("ignored") for hit in hits(revealed))


@pytest.mark.parametrize("show_ignored", [False, True])
def test_excluded_tree_floor_and_project_config_remain_consistent(tree, show_ignored):
    revealable = {"env", "out", "vendor", "coverage"}
    always = {".idea", ".pytest_cache", "bower_components", "Pods", ".terraform"}
    for directory in revealable | always:
        (tree / directory).mkdir()
        (tree / directory / "hit.py").write_text("TARGET_FIELD\n")
    (tree / "config.yaml").write_text("TARGET_FIELD: project setting\n")
    rels = {hit["rel"] for hit in hits(collect(tree, showIgnored=show_ignored))}
    assert "config.yaml" in rels
    assert all(f"{name}/hit.py" not in rels for name in always)
    assert all((f"{name}/hit.py" in rels) == show_ignored for name in revealable)


@pytest.mark.parametrize("scope", ["node_modules", ".ssh", ".ssh/nested", "pairing", ".env", "auth.json"])
def test_explicit_sensitive_scope_cannot_bypass_exclusions(tree, scope):
    (tree / ".ssh" / "nested").mkdir(parents=True)
    (tree / ".ssh" / "nested" / "id_key").write_text("TARGET_FIELD\n")
    (tree / "pairing").mkdir()
    (tree / "pairing" / "token.txt").write_text("TARGET_FIELD\n")
    assert hits(collect(tree, scope="src")), "Readable control must remain searchable"
    assert hits(collect(tree, scope=scope, showIgnored=True)) == []


@pytest.mark.parametrize("directory", [".ssh", ".aws", ".gnupg", "pairing", "mcp-tokens"])
def test_sensitive_root_is_blocked_even_when_ancestor_is_outside_relative_path(tmp_path, directory):
    root = tmp_path / directory / "nested"
    root.mkdir(parents=True)
    (root / "token.txt").write_text("TARGET_FIELD\n")
    assert hits(collect(root, showIgnored=True)) == []


@pytest.mark.platforms("posix")
def test_scope_rejects_internal_and_external_symlinks(tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hit.txt").write_text("TARGET_FIELD\n")
    (tree / "alias").symlink_to(tree / "src", target_is_directory=True)
    (tree / "external").symlink_to(outside, target_is_directory=True)
    (tree / "secret-alias").symlink_to(tree / ".env")
    for scope in ("alias", "alias/**/*.py", "external", "secret-alias"):
        frames = collect(tree, scope=scope, showIgnored=True)
        assert not hits(frames), scope
        assert frames[-1]["type"] == "error", scope


def test_literal_scope_with_brackets_and_spaces_takes_precedence_over_glob(tree):
    (tree / " file[1].py ").write_text("TARGET_FIELD\n")
    frames = collect(tree, scope=" file[1].py ")
    assert [hit["rel"] for hit in hits(frames)] == [" file[1].py "]
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]


@pytest.mark.platforms("posix")
def test_private_profile_link_uses_real_configuration_identity(tmp_path, monkeypatch):
    import hermes_constants

    home = tmp_path / "hermes-home"
    (home / "profiles").mkdir(parents=True)
    actual = tmp_path / "actual-profile"
    actual.mkdir()
    (actual / "config.yaml").write_text("TARGET_FIELD private\n")
    (actual / "public.py").write_text("TARGET_FIELD public\n")
    (home / "profiles" / "linked").symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: home)
    assert [hit["rel"] for hit in hits(collect(actual))] == ["public.py"]


def test_actual_profile_config_is_private_without_hiding_repository_configs(tmp_path, monkeypatch):
    import hermes_constants

    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: home)
    for name, label in (("config.yaml", "private"), ("project/config.yaml", "public")):
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"TARGET_FIELD: {label}\n")
    assert hits(collect(home, scope="config.yaml", showIgnored=True)) == []
    frames = collect(home / "project", showIgnored=True)
    assert [hit["rel"] for hit in hits(frames)] == ["config.yaml"]
    profile = home / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("TARGET_FIELD: private\n")
    assert not hits(collect(profile, showIgnored=True))


@pytest.fixture
def processes(monkeypatch):
    """Observe actual children; do not replace their pipes or their execution."""
    original = asyncio.create_subprocess_exec
    children = []

    async def launch(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return children


@pytest.fixture
def flood_tree(tree):
    # More output than either pipe/StreamReader can buffer, below the file ceiling.
    (tree / "flood.txt").write_text(("TARGET_FIELD " + "x" * 100 + "\n") * 50000)
    return tree


def test_consumer_close_reaps_a_live_blocked_rg(flood_tree, processes):
    from hermes_cli.file_search import SearchRequest, search_files

    async def run():
        stream = search_files(SearchRequest(root=str(flood_tree), query="TARGET_FIELD"), rg_path=shutil.which("rg"))
        try:
            assert (await anext(stream))["type"] == "batch"
            assert processes and any(p.returncode is None for p in processes)
        finally:
            await asyncio.wait_for(stream.aclose(), timeout=5)
        assert all(p.returncode is not None for p in processes)

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancelled", "timeout"])
def test_suspended_consumer_never_disables_cancel_or_deadline(flood_tree, processes, stop):
    from hermes_cli.file_search import SearchRequest, search_files
    from tests.waiting import async_wait_until

    async def run():
        cancelled = asyncio.Event()
        stream = search_files(SearchRequest(root=str(flood_tree), query="TARGET_FIELD", timeoutMs=500),
                              rg_path=shutil.which("rg"), cancelled=cancelled)
        try:
            assert (await anext(stream))["type"] == "batch"
            assert processes and any(p.returncode is None for p in processes)
            if stop == "cancelled":
                cancelled.set()
            await async_wait_until(lambda: all(p.returncode is not None for p in processes),
                                   timeout=3, what=f"rg reaped while consumer suspended ({stop})")
            frames = [frame async for frame in stream]
            assert frames[-1]["type"] == "done" and frames[-1]["reason"] == stop
        finally:
            await stream.aclose()

    asyncio.run(run())


def test_name_regex_uses_ripgrep_dialect_without_python_backtracking(tree):
    frames = collect(tree, query=r"\p{L}+\.py$", mode="name", regex=True)
    assert {hit["rel"] for hit in hits(frames)} == {"src/nested/示例.py"}
    invalid = collect(tree, query=r"(?=app)", mode="name", regex=True)
    assert invalid[-1]["type"] == "error" and invalid[-1]["code"] == "invalid_pattern"


def test_oversized_line_is_explicitly_partial_and_reaps_rg(tree, processes):
    (tree / "huge.txt").write_text("x" * (2 * 1024 * 1024) + "TARGET_FIELD\n")
    frames = collect(tree, scope="huge.txt")
    assert frames[-1]["type"] == "done" and frames[-1]["truncated"]
    assert frames[-1]["reason"] == "output_limit" and frames[-1]["warnings"]
    assert all(p.returncode is not None for p in processes)


def test_result_limit_is_bounded_and_reaps_rg(flood_tree, processes):
    frames = collect(flood_tree, maxResults=2)
    assert len(hits(frames)) == 2
    assert frames[-1]["type"] == "done" and frames[-1]["reason"] == "result_limit"
    assert all(p.returncode is not None for p in processes)


def test_long_unicode_line_has_centered_utf16_ranges(tree):
    prefix = "头😀" * 1000 + " "
    (tree / "long.txt").write_bytes((prefix + "TARGET_FIELD and TARGET_FIELD\r\n").encode())
    frames = collect(tree, scope="long.txt")
    match, = hits(frames)
    assert match["col"] == len(prefix.encode("utf-16-le")) // 2
    assert len(match["text"]) < 500 and match["text"].startswith("…")
    encoded = match["text"].encode("utf-16-le")
    assert [encoded[r["from"] * 2:r["to"] * 2].decode("utf-16-le") for r in match["ranges"]] == [
        "TARGET_FIELD", "TARGET_FIELD",
    ]
    assert "\r" not in match["text"] and "\n" not in match["text"]


def test_invalid_utf8_prefix_keeps_rg_byte_offsets(tree):
    (tree / "legacy.txt").write_bytes(b"\xff TARGET_FIELD\n")
    match, = hits(collect(tree, scope="legacy.txt"))
    assert match["col"] == 2
    assert match["ranges"] == [{"from": 2, "to": 14}]
    assert match["text"] == "\ufffd TARGET_FIELD"


def test_very_long_regex_match_does_not_bypass_snippet_bound(tree):
    (tree / "regex.txt").write_text("x" * 20000 + "\n")
    match, = hits(collect(tree, query="x+", regex=True, scope="regex.txt"))
    assert len(match["text"]) <= 500
    assert match["ranges"][0]["to"] <= len(match["text"].encode("utf-16-le")) // 2


@pytest.mark.parametrize("scope, expected", [
    ("src/*.{py,ts}", {"src/app.ts"}),
    ("src/**/[示a]*.py", {"src/nested/示例.py"}),
    ("src/**/*.py", {"src/nested/示例.py"}),
    ("docs/*.MD", set()),
])
def test_glob_scope_supports_braces_classes_and_case_sensitive_paths(tree, scope, expected):
    frames = collect(tree, scope=scope)
    assert {hit["rel"] for hit in hits(frames)} == expected
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]


@pytest.mark.parametrize("mode", ["name", "content"])
def test_whitespace_and_dash_query_remain_literal(tree, mode):
    name = "--help [a] file.txt"
    (tree / name).write_text("prefix --help [a] suffix\n")
    frames = collect(tree, query="--help [a]", mode=mode, regex=False)
    assert [hit["rel"] for hit in hits(frames)] == [name]
    assert not hits(collect(tree, query="  --help [a]  ", mode=mode))


@pytest.mark.platforms("posix")
def test_posix_backslash_filename_is_not_rewritten(tree):
    name = "literal\\file.txt"
    (tree / name).write_text("TARGET_FIELD\n")
    match, = hits(collect(tree, scope=name))
    assert match["rel"] == name and match["path"] == str(tree / name)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("with_hit", [False, True])
def test_real_rg_partial_io_and_bounded_stderr_do_not_deadlock(tree, with_hit, processes):
    import os

    if os.geteuid() == 0:
        pytest.skip("EACCES cannot be exercised by a privileged test user")
    root = tree / "permissions"
    root.mkdir()
    readable = root / "ok.txt"
    readable.write_text("TARGET_FIELD\n" if with_hit else "NOT_HERE\n")
    denied = []
    for index in range(250):
        path = root / f"denied-{index:04d}.txt"
        path.write_text("TARGET_FIELD\n")
        path.chmod(0)
        denied.append(path)
    try:
        frames = collect(root, timeoutMs=5000)
        if with_hit:
            assert len(hits(frames)) == 1
            assert frames[-1]["type"] == "done" and frames[-1]["reason"] == "partial_error"
            assert frames[-1]["truncated"] and frames[-1]["warnings"]
            assert max(map(len, frames[-1]["warnings"])) <= 1000
        else:
            assert frames[-1]["type"] == "error" and frames[-1]["code"] == "search_failed"
        assert all(p.returncode is not None for p in processes)
    finally:
        for path in denied:
            path.chmod(0o600)


def test_pathological_name_regex_remains_bounded_in_rg(tree):
    (tree / ("a" * 220 + "!")).write_text("no content\n")
    frames = collect(tree, query=r"(a+)+$", mode="name", regex=True, timeoutMs=2000)
    assert not hits(frames)
    assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]


@pytest.mark.platforms("posix")
def test_private_profile_config_is_not_opened_by_rg(tmp_path, monkeypatch):
    import os
    import hermes_constants

    if os.geteuid() == 0:
        pytest.skip("EACCES sentinel requires an unprivileged user")
    home = tmp_path / "private-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: home)
    protected = home / "config.yaml"
    protected.write_text("TARGET_FIELD\n")
    protected.chmod(0)
    (home / "safe.txt").write_text("TARGET_FIELD\n")
    try:
        frames = collect(home, showIgnored=True)
        assert [hit["rel"] for hit in hits(frames)] == ["safe.txt"]
        assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]
        assert frames[-1]["warnings"] == []
    finally:
        protected.chmod(0o600)


@pytest.mark.platforms("posix")
def test_explicit_scope_does_not_read_unrelated_siblings(tree):
    import os

    if os.geteuid() == 0:
        pytest.skip("EACCES sentinel requires an unprivileged user")
    unrelated = tree / "unrelated.txt"
    unrelated.write_text("TARGET_FIELD\n")
    unrelated.chmod(0)
    try:
        frames = collect(tree, scope="src/**/*.py")
        assert hits(frames)
        assert frames[-1]["type"] == "done" and not frames[-1]["truncated"]
        assert frames[-1]["warnings"] == []
    finally:
        unrelated.chmod(0o600)
