"""Tests for consuming audited Dynamic Workflow promotion records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import nio

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)


def _context(tmp_path: Path):
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id="!room:localhost", thread_id=None, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=nio.MatrixRoom(room_id="!room:localhost", own_user_id="@general:localhost"),
        storage_path=None,
    )


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "id": "promoted-flow",
        "name": "Promoted Flow",
        "kind": "workflow",
        "participants": [{"id": "writer", "kind": "ephemeral_agent", "tools": []}],
        "workflow": [
            {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write about {input.topic}."}
        ],
        "permissions": {
            "tools": [],
            "models": [],
            "data": {"matrix_history": "none", "attachments": "none", "knowledge_bases": []},
        },
    }
    spec.update(overrides)
    return spec


def _payload(result: str) -> dict[str, object]:
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    return parsed


def _write_promotion(context, tmp_path: Path, *, spec_hash: str) -> None:
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
                "target_ref": "!room:localhost",
                "spec_hash": spec_hash,
                "workflow_spec_ref": str(tmp_path / "promoted-flow.workflow.json"),
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
    context = _context(tmp_path)
    spec_json = json.dumps(_spec(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    (tmp_path / "promoted-flow.workflow.json").write_text(spec_json, encoding="utf-8")
    _write_promotion(context, tmp_path, spec_hash=hashlib.sha256(spec_json.encode("utf-8")).hexdigest())

    with tool_runtime_context(context):
        listed = _payload(DynamicWorkflowTools().list_workflows(scope="room"))

    assert listed["status"] == "ok"
    assert [workflow["workflow_id"] for workflow in listed["workflows"]] == ["promoted-flow"]


def test_room_scope_rejects_hash_mismatched_dynamic_workflow_promotion(tmp_path: Path) -> None:
    """Promotion projection should fail closed when the spec hash binding is broken."""
    context = _context(tmp_path)
    (tmp_path / "promoted-flow.workflow.json").write_text(json.dumps(_spec()), encoding="utf-8")
    _write_promotion(context, tmp_path, spec_hash="0" * 64)

    with tool_runtime_context(context):
        result = _payload(DynamicWorkflowTools().list_workflows(scope="room"))

    assert result["status"] == "error"
    assert "hash mismatch" in result["message"]