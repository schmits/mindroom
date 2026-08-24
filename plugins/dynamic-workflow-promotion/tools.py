"""Auditable Dynamic Workflow promotion plugin tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolExecutionTarget, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from .workflow_promotion_impl import DynamicWorkflowPromotionTools


@register_tool_with_metadata(
    name="dynamic_workflow_promotion",
    display_name="Dynamic Workflow Promotion",
    description=(
        "Promote pre-validated Dynamic Workflow specs from hash-bound artifacts into scoped, "
        "typed promotion records with immutable audit. Dry-run and preflight never persist."
    ),
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="ShieldCheck",
    icon_color="text-indigo-600",
    config_fields=[
        ConfigField(
            name="state_root",
            label="Promotion State Root",
            type="text",
            required=False,
            default=None,
            description="Optional plugin-private state root. Defaults to runtime plugin state.",
        ),
        ConfigField(
            name="allowed_artifact_roots",
            label="Allowed Artifact Roots",
            type="string[]",
            required=False,
            default=[],
            description="Optional roots for workflow/validation/approval artifact references.",
        ),
        ConfigField(
            name="allowed_approvers",
            label="Allowed Approvers",
            type="string[]",
            required=False,
            default=[],
            description="Optional Matrix IDs allowed to approve promotions. Empty means evidence-bound only.",
        ),
        ConfigField(
            name="approval_ttl_minutes",
            label="Approval TTL Minutes",
            type="number",
            required=False,
            default=1440,
            description="Maximum approval age accepted by apply operations.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="state_root",
            label="Promotion State Root",
            type="text",
            required=False,
            default=None,
            description="Per-agent plugin-private state root.",
        ),
        ConfigField(
            name="allowed_artifact_roots",
            label="Allowed Artifact Roots",
            type="string[]",
            required=False,
            default=[],
            description="Per-agent allowed artifact roots.",
        ),
        ConfigField(
            name="allowed_approvers",
            label="Allowed Approvers",
            type="string[]",
            required=False,
            default=[],
            description="Per-agent Matrix IDs allowed to approve promotions.",
        ),
        ConfigField(
            name="approval_ttl_minutes",
            label="Approval TTL Minutes",
            type="number",
            required=False,
            default=1440,
            description="Per-agent approval TTL.",
        ),
    ],
    dependencies=[],
    function_names=("promote_dynamic_workflow_spec",),
)
def dynamic_workflow_promotion_tools() -> type[DynamicWorkflowPromotionTools]:
    """Return the Dynamic Workflow promotion tools."""
    from .workflow_promotion_impl import DynamicWorkflowPromotionTools

    return DynamicWorkflowPromotionTools