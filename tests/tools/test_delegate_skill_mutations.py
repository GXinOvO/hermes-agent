"""Delegated work reads skills but leaves shared skill mutations to its parent."""
import json
from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context
from tools import skill_manager_tool, skills_tool
from tools.registry import registry

CONTENT = """---
name: scope-fixture
description: Use when testing delegated skill scope.
---

# Scope fixture

Retain the original evidence.
"""


def test_delegated_skill_mutation_is_refused_before_any_write(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    monkeypatch.setattr(skill_manager_tool, 'SKILLS_DIR', root)
    monkeypatch.setattr(skills_tool, 'SKILLS_DIR', root)
    monkeypatch.setattr('agent.skill_utils.get_all_skills_dirs', lambda: [root])
    payload = {'operations': [{'action': 'create', 'name': 'scope-fixture', 'content': CONTENT}]}

    with delegated_child_context():
        raw = registry.dispatch('skill_manage', payload)
        assert isinstance(raw, str)
        result = json.loads(raw)

    assert result.get('success') is False, result
    assert result.get('error_code') == 'delegated_skill_mutation_denied', result
    assert not root.exists(), 'Refusal must precede staging, locks, ledger and skill writes'

    # Restoring the context must not disable the owning agent's normal maintenance.
    raw = registry.dispatch('skill_manage', payload)
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result.get('success') is True, result
    assert (root / 'scope-fixture/SKILL.md').read_text() == CONTENT
    with delegated_child_context():
        raw = registry.dispatch('skill_view', {'name': 'scope-fixture'})
        assert isinstance(raw, str)
        viewed = json.loads(raw)
    assert viewed.get('success') is True, viewed
    assert 'Retain the original evidence.' in viewed['content']


@pytest.mark.parametrize('action', ['create', 'patch', 'delete', 'write_file', 'remove_file'])
def test_descendant_marker_refuses_flat_mutations_without_touching_skill(tmp_path, monkeypatch, action):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    root = tmp_path / 'skills'
    target = root / 'scope-fixture/SKILL.md'
    target.parent.mkdir(parents=True)
    target.write_text(CONTENT)
    monkeypatch.setattr(skill_manager_tool, 'SKILLS_DIR', root)
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(tmp_path / 'owning-board'))
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    raw = registry.dispatch('skill_manage', {
        'action': action, 'name': 'scope-fixture', 'content': CONTENT,
        'old_string': 'original evidence', 'new_string': 'changed evidence',
        'file_path': 'references/new.md', 'file_content': 'changed evidence',
    })
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result.get('success') is False, result
    assert result.get('error_code') == 'delegated_skill_mutation_denied', result
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('bundle', ['skills', 'hermes-cli', 'coding'])
@pytest.mark.parametrize('role', ['leaf', 'orchestrator'])
def test_child_toolsets_keep_reading_without_skill_authoring(bundle, role):
    from model_tools import _select_tool_names
    from tools.delegate_tool_toolsets import _resolve_child_toolsets

    parent = SimpleNamespace(enabled_toolsets=[bundle], disabled_toolsets=[])
    enabled, disabled = _resolve_child_toolsets(parent, None, role)
    names = _select_tool_names(enabled, disabled, quiet_mode=True)
    assert {'skill_view', 'skills_list'} <= names
    assert 'skill_manage' not in names
