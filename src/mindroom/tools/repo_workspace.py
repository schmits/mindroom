"""Ephemeral repo workspace tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolExecutionTarget, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.repo_workspace import RepoWorkspaceTools


@register_tool_with_metadata(
    name="repo_workspace",
    display_name="Repository Workspace",
    description=(
        "Ephemeral, repo-scoped workspace for safe file materialization, editing, status, diff, patch artifacts, "
        "and controlled coding_sandbox handoff. No arbitrary execution, GitHub writes, or ambient secrets."
    ),
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="FolderGit2",
    icon_color="text-emerald-600",
    config_fields=[
        ConfigField(
            name="workspace_root",
            label="Workspace Root",
            type="text",
            required=False,
            default=None,
            description="Directory for ephemeral repo_workspace state. Defaults to ./repo_workspace in the worker workspace.",
        ),
        ConfigField(
            name="allowed_repos",
            label="Allowed GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/repo-sandbox-fixture"],
            description="Owner/name repositories or owner/* patterns this workspace tool may materialize.",
        ),
        ConfigField(
            name="denied_repos",
            label="Denied GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/prod", "schmits/production", "schmits/secrets", "schmits/security"],
            description="Owner/name repositories or owner/* patterns denied before allow rules are evaluated.",
        ),
        ConfigField(
            name="allowed_source_roots",
            label="Allowed Local Source Roots",
            type="string[]",
            required=False,
            default=[],
            description="Local directories from which create_workspace may copy already-materialized repository sources.",
        ),
        ConfigField(
            name="max_ttl_minutes",
            label="Maximum Workspace TTL Minutes",
            type="number",
            required=False,
            default=120,
            description="Maximum allowed workspace lifetime requested by create_workspace.",
        ),
        ConfigField(
            name="allow_network",
            label="Allow Network",
            type="boolean",
            required=False,
            default=False,
            description="Network policy flag recorded in provenance. The MVP does not perform clone/fetch/network operations.",
        ),
        ConfigField(
            name="default_repo",
            label="Default Repository",
            type="text",
            required=False,
            default="schmits/repo-sandbox-fixture",
            description="Default owner/name repository when create_workspace omits repo.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="workspace_root",
            label="Workspace Root",
            type="text",
            required=False,
            default=None,
            description="Per-agent workspace root. Defaults to ./repo_workspace in the worker workspace.",
        ),
        ConfigField(
            name="allowed_repos",
            label="Allowed GitHub Repositories",
            type="string[]",
            required=False,
            default=["schmits/repo-sandbox-fixture"],
            description="Per-agent owner/name repositories or owner/* patterns this workspace tool may materialize.",
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
            name="allowed_source_roots",
            label="Allowed Local Source Roots",
            type="string[]",
            required=False,
            default=[],
            description="Per-agent local directories from which create_workspace may copy repository sources.",
        ),
        ConfigField(
            name="max_ttl_minutes",
            label="Maximum Workspace TTL Minutes",
            type="number",
            required=False,
            default=120,
            description="Per-agent maximum workspace lifetime requested by create_workspace.",
        ),
        ConfigField(
            name="allow_network",
            label="Allow Network",
            type="boolean",
            required=False,
            default=False,
            description="Per-agent network policy flag. The MVP records this but does not perform network operations.",
        ),
        ConfigField(
            name="default_repo",
            label="Default Repository",
            type="text",
            required=False,
            default="schmits/repo-sandbox-fixture",
            description="Per-agent default owner/name repository when create_workspace omits repo.",
        ),
    ],
    dependencies=[],
    function_names=(
        "apply_patch",
        "create_workspace",
        "delete_file",
        "destroy_workspace",
        "export_patch",
        "get_diff",
        "get_status",
        "get_workspace_info",
        "handoff_to_coding_sandbox",
        "list_files",
        "list_workspaces",
        "read_file",
        "write_file",
    ),
)
def repo_workspace_tools() -> type[RepoWorkspaceTools]:
    """Return ephemeral repository workspace tools."""
    from mindroom.custom_tools.repo_workspace import RepoWorkspaceTools

    return RepoWorkspaceTools