"""A legacy bare-custom session must recover the group serving its model on a shared relay."""

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.config import atomic_config_write, get_config_path


RELAY = "https://relay.example/v1"


@pytest.fixture
def relay_config():
    config = {
        "model": {"default": "reasoning-model", "provider": "custom:reasoning"},
        "providers": {
            "writer": {
                "base_url": RELAY,
                "api_key": "writer-fixture-key",
                "model": "writing-model",
                "models": ["writing-model"],
            },
            "reasoning": {
                "base_url": RELAY,
                "api_key": "reasoning-fixture-key",
                "model": "reasoning-model",
                "models": ["reasoning-model"],
            },
        },
    }
    atomic_config_write(get_config_path(), config)
    return config


def test_bare_custom_resume_recovers_the_model_group_on_a_shared_url(relay_config):
    identity = rp.canonical_custom_identity(base_url=RELAY, model="reasoning-model")
    assert identity == "custom:reasoning"
    runtime = rp.resolve_runtime_provider(requested=identity, target_model="reasoning-model")
    assert runtime["api_key"] == "reasoning-fixture-key"


@pytest.mark.parametrize("model", ["writing-model", "unlisted-model"])
def test_explicit_group_wins_even_when_a_sibling_lists_the_model(relay_config, model):
    assert rp.canonical_custom_identity(
        base_url=RELAY, model=model, requested="custom:reasoning"
    ) == "custom:reasoning"


def test_model_recovery_never_picks_the_requested_entry_after_it_moves(relay_config):
    relay_config["providers"]["reasoning"]["base_url"] = "https://new.example/v1"
    atomic_config_write(get_config_path(), relay_config)
    assert rp.canonical_custom_identity(
        base_url=RELAY, model="reasoning-model", requested="custom:reasoning"
    ) == "custom:writer"
