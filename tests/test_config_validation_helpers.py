"""Focused tests for shared config validation helpers."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ToolConfigEntry
from mindroom.config.validation import duplicate_items, validate_history_limit_choice
from mindroom.constants import resolve_runtime_paths


def test_duplicate_items_preserves_first_duplicate_encounter_order() -> None:
    """Duplicate reporting should follow the first repeated occurrence."""
    assert duplicate_items(["alpha", "beta", "gamma", "beta", "alpha", "beta"]) == ["beta", "alpha"]


def test_history_limit_choice_rejects_both_limits_with_existing_message() -> None:
    """Shared history validation should keep the existing user-facing message."""
    with pytest.raises(ValueError, match="num_history_runs and num_history_messages are mutually exclusive"):
        validate_history_limit_choice(num_history_runs=2, num_history_messages=10)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ToolConfigEntry(name="shell", initial=True),
            "Tool entry initial=true requires defer=true",
        ),
        (
            lambda: AgentConfig(
                display_name="Code",
                knowledge_bases=["docs", "runbooks", "docs", "runbooks"],
            ),
            "Duplicate knowledge bases are not allowed: docs, runbooks",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code", "research", "code", "research"],
            ),
            "Duplicate agents are not allowed in a team: code, research",
        ),
        (
            lambda: CultureConfig(agents=["code", "research", "code", "research"]),
            "Duplicate agents are not allowed in a culture: code, research",
        ),
        (
            lambda: AuthorizationConfig(
                aliases={
                    "@alice:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                    "@bob:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                },
            ),
            "Duplicate bridge aliases are not allowed: @telegram_1:example.com, @discord_1:example.com",
        ),
    ],
)
def test_duplicate_validators_preserve_error_order(factory: object, match: str) -> None:
    """Config duplicate validators should preserve encounter-ordered duplicate names."""
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: DefaultsConfig(num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: AgentConfig(display_name="Code", num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code"],
                num_history_runs=2,
                num_history_messages=10,
            ),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
    ],
)
def test_history_limit_validators_preserve_error_message(factory: object, match: str) -> None:
    """Defaults, agents, and teams should reject ambiguous history limits consistently."""
    with pytest.raises(ValueError, match=match):
        factory()


def _runtime_paths_for_validation() -> object:
    """Return runtime paths for catalog-aware config validation."""
    return resolve_runtime_paths(config_path="config.yaml", process_env={})


def test_github_dev_coding_sandbox_fixture_includes_repo_sandbox_and_production_github_dev_does_not() -> None:
    """The non-production GitHub Dev sandbox fixture should be the only GitHub Dev config with repo_sandbox."""
    config = Config.validate_with_runtime(
        {
            "agents": {
                "github_dev": {
                    "display_name": "GitHub Dev",
                    "role": "Production GitHub development agent",
                    "tools": ["github"],
                },
                "github_dev_coding_sandbox": {
                    "display_name": "GitHub Dev Coding Sandbox",
                    "role": "Non-production GitHub Dev coding sandbox fixture",
                    "tools": ["github", "repo_sandbox"],
                },
            },
        },
        _runtime_paths_for_validation(),
    )

    assert "repo_sandbox" in config.agents["github_dev_coding_sandbox"].tool_names
    assert "repo_sandbox" not in config.agents["github_dev"].tool_names


def test_github_dev_coding_sandbox_repo_sandbox_structured_fields_validate() -> None:
    """Structured repo_sandbox entries should accept supported fields in the sandbox fixture."""
    config = Config.validate_with_runtime(
        {
            "agents": {
                "github_dev": {
                    "display_name": "GitHub Dev",
                    "role": "Production GitHub development agent",
                    "tools": ["github"],
                },
                "github_dev_coding_sandbox": {
                    "display_name": "GitHub Dev Coding Sandbox",
                    "role": "Non-production GitHub Dev coding sandbox fixture",
                    "tools": [
                        "github",
                        {
                            "repo_sandbox": {
                                "sandbox_root": "./repo_sandbox",
                                "allowed_repos": ["schmits/repo-sandbox-fixture"],
                                "denied_repos": ["schmits/prod"],
                                "allowed_test_commands": ["pytest -q"],
                                "default_repo": "schmits/repo-sandbox-fixture",
                            },
                        },
                    ],
                },
            },
        },
        _runtime_paths_for_validation(),
    )

    assert config.agents["github_dev_coding_sandbox"].get_tool_overrides("repo_sandbox") == {
        "sandbox_root": "./repo_sandbox",
        "allowed_repos": ["schmits/repo-sandbox-fixture"],
        "denied_repos": ["schmits/prod"],
        "allowed_test_commands": ["pytest -q"],
        "default_repo": "schmits/repo-sandbox-fixture",
    }
    assert config.agents["github_dev"].get_tool_overrides("repo_sandbox") is None


def test_github_dev_coding_sandbox_repo_sandbox_rejects_unknown_structured_field() -> None:
    """Runtime validation should reject unsupported structured repo_sandbox fields."""
    with pytest.raises(ValueError, match=r"repo_sandbox\.unknown_field: unknown authored override field"):
        Config.validate_with_runtime(
            {
                "agents": {
                    "github_dev_coding_sandbox": {
                        "display_name": "GitHub Dev Coding Sandbox",
                        "tools": [{"repo_sandbox": {"unknown_field": True}}],
                    },
                },
            },
            _runtime_paths_for_validation(),
        )
