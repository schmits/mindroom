"""Pre-seeded local repo sandbox tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolExecutionTarget, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.repo_sandbox import RepoSandboxTools


@register_tool_with_metadata(
    name="repo_sandbox",
    display_name="Repository Sandbox",
    description=(
        "Allowlisted pre-seeded local repository inspect/edit/test workflow confined to a sandbox directory. "
        "No GitHub clone, fetch, or authenticated network access is performed; edit, write, and test calls require explicit confirm_write=true."
    ),
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="GitBranch",
    icon_color="text-emerald-600",
    config_fields=[
        ConfigField(
            name="sandbox_root",
            label="Sandbox Root",
            type="text",
            required=False,
            default=None,
            description="Directory containing pre-seeded allowlisted repositories. Defaults to ./repo_sandbox in the worker workspace.",
        ),
        ConfigField(
            name="allowed_repos",
            label="Allowed GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/repo-sandbox-fixture"],
            description="Owner/name repositories or owner/* repository patterns this tool may access when already pre-seeded locally.",
        ),
        ConfigField(
            name="denied_repos",
            label="Denied GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/prod", "schmits/production", "schmits/secrets", "schmits/security"],
            description="Owner/name repositories or owner/* patterns that are denied before allow rules are evaluated.",
        ),
        ConfigField(
            name="allowed_test_commands",
            label="Allowed Test Commands",
            type="string[]",
            required=False,
            default=["pytest -q", "python -m pytest -q", "npm test", "npm run test", "pnpm test"],
            description="Exact test commands allowed by run_tests. Commands run without a shell.",
        ),
        ConfigField(
            name="default_repo",
            label="Default Repository",
            type="text",
            required=False,
            default="schmits/repo-sandbox-fixture",
            description="Default owner/name repository when a call omits repo.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="sandbox_root",
            label="Sandbox Root",
            type="text",
            required=False,
            default=None,
            description="Per-agent sandbox root. Defaults to ./repo_sandbox in the worker workspace.",
        ),
        ConfigField(
            name="allowed_repos",
            label="Allowed GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/repo-sandbox-fixture"],
            description="Per-agent owner/name repositories or owner/* repository patterns this tool may access when already pre-seeded locally.",
        ),
        ConfigField(
            name="denied_repos",
            label="Denied GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/prod", "schmits/production", "schmits/secrets", "schmits/security"],
            description="Per-agent owner/name repositories or owner/* patterns denied before allow rules are evaluated.",
        ),
        ConfigField(
            name="allowed_test_commands",
            label="Allowed Test Commands",
            type="string[]",
            required=False,
            default=["pytest -q", "python -m pytest -q", "npm test", "npm run test", "pnpm test"],
            description="Per-agent exact test commands allowed by run_tests.",
        ),
        ConfigField(
            name="default_repo",
            label="Default Repository",
            type="text",
            required=False,
            default="schmits/repo-sandbox-fixture",
            description="Per-agent default owner/name repository when a call omits repo.",
        ),
    ],
    dependencies=[],
    function_names=("clone_or_update", "edit_file", "grep", "list_files", "read_file", "run_tests", "status", "write_file"),
)
def repo_sandbox_tools() -> type[RepoSandboxTools]:
    """Return safe repository sandbox tools."""
    from mindroom.custom_tools.repo_sandbox import RepoSandboxTools

    return RepoSandboxTools