"""Focused tests for Dynamic Workflow promotion consumption."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)


def _workflow_spec(*, workflow_id: str = "promoted-flow", name: str = "Promoted Flow") -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": workflow_id,
        "name": name,
        "description": "Promotion bridge test workflow.",
        "kind": "workflow",
        "inputs": {"type": "object", "properties": {}},
        "participants": [
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Writer",
                "model": "claude-sonnet-4-6",
                "tools": [],
            },
        ],
        "workflow": [
            {
                "id": "write",
                "type": "agent_step",
                "participant": "writer",
                "prompt": "Write a short response.",
            },
        ],
        "outputs": [],
        "permissions": {
            "max_runtime_seconds": 1800,
            "max_concurrent_agents": 1,
            "max_total_agents": 1,
            "models": ["claude-sonnet-4-6"],
            "tools": [],
            "data": {"matrix_history": "none", "attachments": "none", "knowledge_bases": []},
        },
    }


def _make_context(tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={**dict(runtime_paths.process_env), "MINDROOM_PUBLIC_URL": "https://acme.mindroom.chat"},
        env_file_values=runtime_paths.env_file_values,
    )
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id="!room:localhost"),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def _write_promotion(
    context: ToolRuntimeContext,
    spec_path: Path,
    spec_hash: str,
    *,
    target_ref: str = "!room:localhost",
) -> None:
    promotion_dir = context.runtime_paths.storage_root / "plugins" / "dynamic-workflow-promotion" / "promotions"
    promotion_dir.mkdir(parents=True, exist_ok=True)
    (promotion_dir / "promotion.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "promotion_id": "promotion",
                "workflow_id": "promoted-flow",
                "workflow_name": "Promoted Flow",
                "status": "active",
                "target_scope": "room",
                "target_ref": target_ref,
                "spec_hash": spec_hash,
                "workflow_spec_ref": str(spec_path),
                "approved_by": "@approver:localhost",
                "reason": "approved test promotion",
                "created_by": "agent_builder",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_room_scope_consumes_active_dynamic_workflow_promotion(tmp_path: Path) -> None:
    """Room-scoped lookup should project active audited promotion records into the workflow store."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec()
    spec_path = tmp_path / "promoted-flow.workflow.json"
    spec_json = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    spec_path.write_text(spec_json, encoding="utf-8")
    _write_promotion(context, spec_path, hashlib.sha256(spec_json.encode("utf-8")).hexdigest())

    with tool_runtime_context(context):
        listed = json.loads(tool.list_workflows(scope="room"))

    assert listed["status"] == "ok"
    assert listed["scope"] == "room"
    assert listed["owner_id"] == "!room:localhost"
    assert [workflow["workflow_id"] for workflow in listed["workflows"]] == ["promoted-flow"]


def test_room_scope_rejects_hash_mismatched_dynamic_workflow_promotion(tmp_path: Path) -> None:
    """Promotion projection should fail closed when the spec hash binding is broken."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec_path = tmp_path / "promoted-flow.workflow.json"
    spec_path.write_text(json.dumps(_workflow_spec()), encoding="utf-8")
    _write_promotion(context, spec_path, "0" * 64)

    with tool_runtime_context(context):
        result = json.loads(tool.list_workflows(scope="room"))

    assert result["status"] == "error"
    assert "hash mismatch" in result["message"]


def test_room_scope_promotion_does_not_enable_direct_mutation(tmp_path: Path) -> None:
    """Promotion consumption should not reopen direct shared-scope create/update paths."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(tool.create_workflow(spec=_workflow_spec(), scope="room"))

    assert result["status"] == "error"
    assert "requires Dynamic Workflow approval policy" in result["message"]