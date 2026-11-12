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


def test_js_suite_provisions_ripgrep_from_the_locked_toolchain():
    from scripts.ci.setup_toolchain import packages

    job = _workflows()["js-tests.yml"]["jobs"]["check"]
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-pm")
    requested = [name.strip() for name in setup["with"].get("packages", "").split(",") if name.strip()]
    assert "ripgrep" in packages(setup["with"]["toolchain"], requested)


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
