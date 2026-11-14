"""Process death cannot leave new plugin code paired with the old dependency selection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("committed", [False, True])
def test_boot_recovers_plugin_publication_after_process_death(tmp_path, committed):
    home = tmp_path / "home"
    target = home / "plugins" / "example"
    target.mkdir(parents=True)
    (target / "__init__.py").write_text("old code", encoding="utf-8")
    metadata = target.parent / ".install-metadata.json"
    metadata.write_text('{"example":{"revision":"old"}}\n', encoding="utf-8")
    previous = metadata.read_bytes()
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "__init__.py").write_text("new code", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    program = '''
from pathlib import Path
import os,sys
from pm.publication import StagedPlugin
from pm.store import tree_digest
from pm.environments import runtime_facts_path
from pm.lock import Facts
project,staged,target = map(Path,sys.argv[1:4])
StagedPlugin({"staged": str(staged), "target": str(target), "target_digest": tree_digest(target),
              "old_metadata": {"example":{"revision":"old"}},
              "new_metadata": {"example":{"revision":"new"}}}).publish(project)
if sys.argv[4] == "True":
    Facts(runtime_facts_path(project)).record_state("venv","new",[])
os._exit(17)
'''
    child = subprocess.run([sys.executable, "-c", program, str(project), str(staged), str(target), str(committed)],
                           env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
    assert child.returncode == 17, child.stderr
    recovery = '''
from pathlib import Path
import sys
from hermes_cli.runtime_state import runtime_lock,recover_publication
project=Path(sys.argv[1])
with runtime_lock(project):
    recover_publication(project)
    recover_publication(project)
'''
    result = subprocess.run([sys.executable, "-c", recovery, str(project)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert (target / "__init__.py").read_text(encoding="utf-8") == ("new code" if committed else "old code")
    assert json.loads(metadata.read_text(encoding="utf-8"))["example"]["revision"] == ("new" if committed else "old")
    if not committed:
        assert metadata.read_bytes() == previous
    assert not list(target.parent.glob(".previous-*"))


def test_recovery_preserves_a_later_cooperating_writer(tmp_path, monkeypatch):
    from pm.environments import install_state_dir
    from pm.plugin_target_lock import target_writer_lock

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = home / "plugins" / "example"
    target.mkdir(parents=True)
    (target / "plugin.yaml").write_text("name: example\n", encoding="utf-8")
    (target / "code.py").write_text("old code", encoding="utf-8")
    (home / "plugins" / ".install-metadata.json").write_text(
        '{"example":{"revision":"old"}}\n', encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "plugin.yaml").write_text("name: example\n", encoding="utf-8")
    (staged / "code.py").write_text("new code", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(REPO)}
    publish = (
        "from pathlib import Path\n"
        "import os,sys\n"
        "from pm.publication import StagedPlugin\n"
        "from pm.store import tree_digest\n"
        "project,staged,target=map(Path,sys.argv[1:4])\n"
        "StagedPlugin({'staged':str(staged),'target':str(target),'target_digest':tree_digest(target),"
        "'old_metadata':{'example':{'revision':'old'}},"
        "'new_metadata':{'example':{'revision':'new'}}}).publish(project)\n"
        "os._exit(17)\n"
    )
    child = subprocess.run([sys.executable, "-B", "-c", publish, str(project), str(staged), str(target)],
                           cwd=REPO, env=env, capture_output=True, text=True, check=False)
    assert child.returncode == 17, child.stderr
    journal = install_state_dir(project) / "publication.json"
    record = json.loads(journal.read_text(encoding="utf-8"))
    backup = Path(record["backup"])
    with target_writer_lock(target):
        (target / "code.py").write_text("later writer", encoding="utf-8")

    recover = (
        "from pathlib import Path\n"
        "import sys\n"
        "from hermes_cli.runtime_state import runtime_lock,recover_publication\n"
        "project=Path(sys.argv[1])\n"
        "with runtime_lock(project):\n"
        "    recover_publication(project)\n"
    )
    result = subprocess.run([sys.executable, "-B", "-c", recover, str(project)],
                            cwd=REPO, env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "target changed" in result.stderr
    assert journal.is_file() and backup.is_dir()
    assert (target / "code.py").read_text(encoding="utf-8") == "later writer"
