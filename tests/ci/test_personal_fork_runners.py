import json
from collections import deque
from pathlib import Path

from ruamel.yaml import YAML


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
HOSTED = {
    "ubuntu-latest",
    "ubuntu-24.04",
    "ubuntu-24.04-arm",
    "windows-2025",
    "windows-11-arm",
    "macos-latest",
    "macos-15",
    "macos-15-intel",
    "xcode-27",
    "macos-26",
}


def _workflows():
    yaml = YAML(typ="base")
    return {path.name: yaml.load(path.read_text(encoding="utf-8"))
            for path in WORKFLOWS.glob("*.y*ml")}


def _pull_request_closure(workflows):
    pending = deque(name for name, doc in workflows.items()
                    if "pull_request" in ((doc or {}).get("on") or {}))
    seen = set()
    while pending:
        name = pending.popleft()
        if name in seen:
            continue
        seen.add(name)
        for job in (workflows[name].get("jobs") or {}).values():
            uses = (job or {}).get("uses") or ""
            if uses.startswith("./.github/workflows/"):
                pending.append(uses.rsplit("/", 1)[-1])
    return seen


def _runners(job):
    runner = job.get("runs-on")
    if runner is None:
        return []
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if runner == "${{ matrix.runner }}":
        return list(matrix.get("runner") or []) + [cell["runner"] for cell in matrix.get("include", [])]
    if runner == "${{ matrix.target.runner }}":
        return [target["runner"] for target in matrix["target"]]
    return [runner]


def test_pull_request_workflows_have_public_hosted_runners():
    workflows = _workflows()
    violations = []
    for name in sorted(_pull_request_closure(workflows)):
        for job_id, job in (workflows[name].get("jobs") or {}).items():
            for runner in _runners(job or {}):
                if runner not in HOSTED:
                    violations.append(f"{name}:{job_id}: {runner}")
    assert not violations, "\n".join(violations)


def test_core_suite_parallelism_fits_standard_linux_runner():
    job = _workflows()["tests.yml"]["jobs"]["test"]
    run = next(step for step in job["steps"] if step.get("name") == "Run tests")
    slices = job["strategy"]["matrix"]["slice"]
    assert int(run["env"]["HERMES_TEST_WORKERS"]) <= 4
    assert run["env"]["HERMES_TEST_SLICE"] == f"${{{{ matrix.slice }}}}/{len(slices)}"
    assert list(map(int, slices)) == list(range(1, len(slices) + 1))


def test_core_suite_installs_the_platform_sdks_and_bubblewrap():
    job = _workflows()["tests.yml"]["jobs"]["test"]
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-pm")
    extras = set(json.loads(setup["with"]["extras"]))
    assert {"messaging", "matrix", "dingtalk", "teams"} <= extras
    assert any("bubblewrap" in step.get("run", "") for step in job["steps"])


def test_upgrade_job_fetches_official_tags_without_overwriting(tmp_path):
    import os
    import subprocess

    from tests.ci import _gha_expr as gha

    upstream = tmp_path / "upstream"
    checkout = tmp_path / "checkout"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"),
               GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="tester@example.org",
               GIT_COMMITTER_NAME="Tester", GIT_COMMITTER_EMAIL="tester@example.org")

    def git(*args, cwd=None, check=True):
        return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env, capture_output=True,
                              text=True, check=check, timeout=30)

    git("init", "-q", "-b", "main", upstream)
    git("commit", "-q", "--allow-empty", "-m", "release", cwd=upstream)
    git("tag", "v0.1.0", cwd=upstream)
    git("commit", "-q", "--allow-empty", "-m", "later", cwd=upstream)
    git("clone", "-q", "--no-tags", upstream, checkout)
    git("config", "--local", f"url.{upstream}.insteadOf", "https://github.com/NousResearch/hermes-agent.git", cwd=checkout)

    job = _workflows()["tests.yml"]["jobs"]["e2e-upgrade"]
    steps = [step for step in job["steps"] if step.get("name") == "Fetch upstream release tags for fork"]
    assert len(steps) == 1
    step = steps[0]
    assert gha.condition(step["if"], {"github": {"repository": "GXinOvO/hermes-agent"}})
    assert not gha.condition(step["if"], {"github": {"repository": "another/hermes-agent"}})
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=checkout,
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert git("tag", "--merged", "HEAD", "--no-contains", "HEAD", "--list", "v[0-9]*",
               cwd=checkout).stdout.splitlines() == ["v0.1.0"]

    git("tag", "-f", "v0.1.0", "HEAD", cwd=checkout)
    original = git("rev-parse", "refs/tags/v0.1.0", cwd=checkout).stdout.strip()
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=checkout,
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert git("rev-parse", "refs/tags/v0.1.0", cwd=checkout).stdout.strip() == original


def test_js_suite_provisions_ripgrep_from_the_locked_toolchain():
    from scripts.ci.setup_toolchain import packages

    job = _workflows()["js-tests.yml"]["jobs"]["check"]
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-pm")
    requested = [name.strip() for name in setup["with"].get("packages", "").split(",") if name.strip()]
    assert "ripgrep" in packages(setup["with"]["toolchain"], requested)


def test_contributor_guard_uses_dev_for_fork_dispatch_and_preserves_pr_base():
    from tests.ci import _gha_expr as gha

    job = _workflows()["repo-guards.yml"]["jobs"]["guards"]
    step = next(step for step in job["steps"] if step.get("id") == "check-emails")
    expression = step["env"]["_HERMES_PR_BASE_SHA"]
    for repository, base, expected in (
        ("GXinOvO/hermes-agent", "", "origin/dev"),
        ("GXinOvO/hermes-agent", "pr-base", "pr-base"),
        ("NousResearch/hermes-agent", "", ""),
        ("NousResearch/hermes-agent", "pr-base", "pr-base"),
    ):
        context = {"github": {"repository": repository, "event": {"pull_request": {"base": {"sha": base}}}}}
        assert gha.render(expression, context) == expected


def test_windows_x64_and_arm_slices_cover_the_same_files_once():
    from scripts.run_tests_parallel import _slice_files

    job = _workflows()["tests-os.yml"]["jobs"]["os-tests"]
    cells = job["strategy"]["matrix"]["include"]
    files = [Path("/repo/tests") / f"test_{n}.py" for n in range(101)]
    for runner in ("windows-2025", "windows-11-arm"):
        specs = [cell["slice"] for cell in cells if cell["runner"] == runner]
        indexes, counts = zip(*(map(int, spec.split("/")) for spec in specs))
        assert len(set(counts)) == 1
        assert sorted(indexes) == list(range(1, counts[0] + 1))
        chosen = [path for index in indexes for path in _slice_files(files, index, counts[0], {}, Path("/repo"))]
        assert sorted(chosen) == sorted(files)
