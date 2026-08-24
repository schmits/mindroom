"""Tests for Dynamic Workflow storage and tools."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest
import yaml
from agno.factory import RequestContext
from agno.run.agent import RunOutput, RunStatus
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.workflow import Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.auth import AgentReplyPermission, AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.dynamic_workflow import _MINIMAL_SPEC_EXAMPLE, DynamicWorkflowTools
from mindroom.dynamic_workflows.agno_adapter import build_agno_workflow_factory
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError, execute_workflow_spec
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.dynamic_workflows.validation import DynamicWorkflowError
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import _matching_tool_approval_rule
from mindroom.tool_system.automation_approval import NEVER_PREAPPROVE_TOOLKITS, build_automation_approval_config
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


def _fake_stream_agent(
    *,
    content: str,
    status: RunStatus = RunStatus.completed,
    on_run: Callable[..., None] | None = None,
) -> SimpleNamespace:
    """Build a fake Agent matching the streaming participant run contract.

    ``_arun_agent`` calls ``agent.arun(..., stream=True, yield_run_output=True)`` (not awaited)
    and consumes the event iterator, treating the final ``RunOutput`` as the result.
    """

    def arun(prompt: str, *, user_id: str, session_id: str, **_kwargs: object) -> AsyncIterator[RunOutput]:
        if on_run is not None:
            on_run(prompt, user_id=user_id, session_id=session_id)

        async def _events() -> AsyncIterator[RunOutput]:
            yield RunOutput(content=content, status=status)

        return _events()

    return SimpleNamespace(arun=arun)


def _workflow_spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "id": "competitor-research-report",
        "name": "Competitor Research Report",
        "description": "Create a cited HTML report about competitors.",
        "kind": "workflow",
        "inputs": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "participants": [
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "model": "claude-sonnet-4-6",
                "tools": [],
            },
        ],
        "workflow": [
            {
                "id": "write",
                "type": "agent_step",
                "participant": "writer",
                "prompt": "Write a cited report in Markdown.",
            },
        ],
        "outputs": [{"id": "report_html", "type": "html_report", "from_step": "write"}],
        "permissions": {
            "max_runtime_seconds": 1800,
            "max_concurrent_agents": 4,
            "max_total_agents": 16,
            "models": ["claude-sonnet-4-6"],
            "tools": [],
            "data": {
                "matrix_history": "none",
                "attachments": "none",
                "knowledge_bases": [],
            },
        },
    }
    spec.update(overrides)
    return spec


def _make_context(tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={
            **dict(runtime_paths.process_env),
            "MINDROOM_PUBLIC_URL": "https://acme.mindroom.chat",
        },
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
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def _make_multi_agent_context(tmp_path: Path, *, room_agents: list[str]) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"]),
                "specialist": AgentConfig(display_name="Specialist Agent"),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=registry.current_id("general").full_id)
    for agent_name in room_agents:
        room.add_member(registry.current_id(agent_name).full_id, config.agents[agent_name].display_name, None)
    room.members_synced = True
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=room,
        storage_path=None,
    )


def _tool_payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_dynamic_workflow_tool_registered() -> None:
    """Dynamic Workflow tool metadata should be visible to config and dashboard surfaces."""
    metadata = TOOL_METADATA["dynamic_workflow"]

    assert metadata.display_name == "Dynamic Workflows"
    assert metadata.function_names == (
        "create_workflow",
        "validate_workflow",
        "update_workflow",
        "run_workflow",
        "get_workflow_run",
        "list_workflows",
        "list_workflow_revisions",
    )


def test_create_workflow_persists_immutable_revision(tmp_path: Path) -> None:
    """Creating a workflow should write a pointer file and immutable revision file."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec()

    summary = store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general", reason="initial")

    assert summary.workflow_id == "competitor-research-report"
    assert summary.scope == "agent"
    assert summary.owner_id == "general"
    assert summary.name == "Competitor Research Report"
    assert summary.revision_count == 1
    assert summary.active_revision.startswith("rev_")
    loaded = store.get_workflow(workflow_id="competitor-research-report", scope="agent", owner_id="general")
    assert loaded.active_revision == summary.active_revision
    revision = store.get_revision(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        revision=summary.active_revision,
    )
    assert revision.spec == spec
    assert revision.reason == "initial"
    assert revision.created_by == "general"


def test_update_workflow_creates_new_revision_without_mutating_old_one(tmp_path: Path) -> None:
    """Updating a workflow should keep previous immutable revisions intact."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec()
    initial = store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")

    updated = store.update_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        patch={"description": "Updated description", "permissions": {"max_runtime_seconds": 900}},
        updated_by="general",
        reason="tighten runtime",
    )

    assert updated.active_revision != initial.active_revision
    assert updated.description == "Updated description"
    assert updated.revision_count == 2
    old_revision = store.get_revision(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        revision=initial.active_revision,
    )
    new_revision = store.get_revision(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        revision=updated.active_revision,
    )
    assert old_revision.spec["description"] == "Create a cited HTML report about competitors."
    assert new_revision.spec["description"] == "Updated description"
    assert new_revision.spec["permissions"]["max_runtime_seconds"] == 900
    assert new_revision.reason == "tighten runtime"


def test_concurrent_update_workflow_creates_distinct_revisions(tmp_path: Path) -> None:
    """Concurrent updates should not clobber immutable revision artifacts."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=_workflow_spec(), scope="agent", owner_id="general", created_by="general")

    def update(index: int) -> str:
        summary = store.update_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            patch={"description": f"Updated description {index}"},
            updated_by=f"agent-{index}",
            reason=f"concurrent update {index}",
        )
        return summary.active_revision

    with ThreadPoolExecutor(max_workers=4) as executor:
        revisions = list(executor.map(update, range(4)))

    assert len(set(revisions)) == 4
    listed = store.list_revisions(workflow_id="competitor-research-report", scope="agent", owner_id="general")
    assert len(listed) == 5
    assert {revision.created_by for revision in listed if revision.reason and revision.reason.startswith("concurrent")} == {
        "agent-0",
        "agent-1",
        "agent-2",
        "agent-3",
    }


def test_update_workflow_rejects_workflow_id_changes(tmp_path: Path) -> None:
    """A patch cannot move an existing workflow to another ID."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=_workflow_spec(), scope="agent", owner_id="general", created_by="general")

    with pytest.raises(DynamicWorkflowError, match="cannot change workflow id"):
        store.update_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            patch={"id": "different-id"},
            updated_by="general",
        )


def test_run_workflow_writes_run_record_and_private_html_report(tmp_path: Path) -> None:
    """Running a workflow should persist run metadata, outputs, and a private report artifact."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "html_report", "from_step": "research"}],
    )
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store)

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@user:localhost",
        base_url="https://mindroom.test",
    )

    assert run.status == "completed"
    assert run.outputs == {"brief": "Research brief for Agno factories."}
    assert run.report_url.startswith(
        "https://mindroom.test/reports/private/agent/general/competitor-research-report/run_",
    )
    loaded_run = store.get_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded_run.requested_by == "@user:localhost"
    assert loaded_run.outputs == run.outputs
    assert loaded_run.artifacts["brief"].endswith(".html")
    artifact_path = store.private_report_path(loaded_run.artifacts["brief"])
    assert artifact_path.read_text(encoding="utf-8") == "Research brief for Agno factories."


def test_run_workflow_persists_failed_run_when_completion_persistence_fails(tmp_path: Path) -> None:
    """Completed run persistence failures should still record failure metadata for auditability."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(
        workflow=[{"id": "research", "type": "transform_step", "template": "Research brief for {input.topic}."}],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store)
    original_save_run = store.save_run
    calls = 0

    def flaky_save_run(run: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DynamicWorkflowError("disk full")
        original_save_run(run)

    with patch.object(store, "save_run", side_effect=flaky_save_run):
        run = service.run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "Agno factories"},
            requested_by="@user:localhost",
            base_url="https://mindroom.test",
        )

    assert run.status == "failed"
    assert run.error == "Failed to persist completed Dynamic Workflow run: disk full"
    assert calls == 2
    loaded_run = store.get_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded_run.status == "failed"
    assert loaded_run.outputs == {"brief": "Research brief for Agno factories."}


def test_run_workflow_rejects_missing_required_input_before_execution(tmp_path: Path) -> None:
    """Input schema validation should reject bad input before any steps run."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec()
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store)

    with pytest.raises(DynamicWorkflowError, match="missing required property: topic"):
        service.run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={},
            requested_by="@user:localhost",
            base_url="https://mindroom.test",
        )


def test_validate_workflow_spec_rejects_invalid_input_schema_type() -> None:
    """Input schemas should allow JSON Schema primitive type strings only."""
    spec = _workflow_spec(inputs={"type": "bogus"})

    with pytest.raises(DynamicWorkflowError, match="inputs.type must be one of"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_empty_input_schema_type_list() -> None:
    """Input schemas should reject empty type lists."""
    spec = _workflow_spec(inputs={"type": []})

    with pytest.raises(DynamicWorkflowError, match="inputs.type must not be empty"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_requires_supported_schema_version() -> None:
    """Workflow specs must declare the supported schema version."""
    spec = _workflow_spec(schema_version=2)

    with pytest.raises(DynamicWorkflowError, match="schema_version must be 1"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unsupported_input_schema_keywords() -> None:
    """Input schemas should fail closed against unsupported JSON Schema features."""
    spec = _workflow_spec(inputs={"type": "object", "properties": {}, "patternProperties": {"^x-": {"type": "string"}}})

    with pytest.raises(DynamicWorkflowError, match="inputs contains unsupported JSON Schema keyword 'patternProperties'"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_run_workflow_enforces_input_schema_enum(tmp_path: Path) -> None:
    """Runtime input validation should enforce supported JSON Schema enum constraints."""
    spec = _workflow_spec(
        inputs={
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string", "enum": ["Agno"]}},
        },
        workflow=[{"id": "research", "type": "transform_step", "template": "Research brief for {input.topic}."}],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store)

    with pytest.raises(DynamicWorkflowError, match="input.topic must be one of"):
        service.run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "MindRoom"},
            requested_by="@user:localhost",
            base_url="https://mindroom.test",
        )


def test_validate_workflow_spec_rejects_excessive_agent_steps() -> None:
    """Workflow specs should respect the declared max total agents budget."""
    spec = _workflow_spec(
        permissions={**_workflow_spec()["permissions"], "max_total_agents": 1},
        workflow=[
            {"id": f"write-{index}", "type": "agent_step", "participant": "writer", "prompt": "Write."}
            for index in range(2)
        ],
    )

    with pytest.raises(DynamicWorkflowError, match="exceeds permissions.max_total_agents"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_normalizes_tool_grants() -> None:
    """Workflow tool grants should normalize dashes and underscores."""
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "tools": ["web-search"],
            },
        ],
        permissions={**_workflow_spec()["permissions"], "tools": ["web_search"]},
    )

    DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_participant_tool_not_granted_by_permissions() -> None:
    """Workflow participants should not receive tools outside the workflow permission grant."""
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "tools": ["memory"],
            },
        ],
        permissions={**_workflow_spec()["permissions"], "tools": ["web_search"]},
    )

    with pytest.raises(DynamicWorkflowError, match="not granted by permissions.tools"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_tool_policy_rejects_unknown_tool() -> None:
    """Workflow tool policy should fail closed when a tool is unknown to local metadata."""
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "tools": ["unknown_tool"],
            },
        ],
        permissions={**_workflow_spec()["permissions"], "tools": ["unknown_tool"]},
    )

    with pytest.raises(DynamicWorkflowError, match="unknown tool"):
        dynamic_workflow_module._validate_workflow_tool_policy(spec)


def test_validate_workflow_tool_policy_rejects_each_restricted_tool() -> None:
    """Restricted and broad-capability tools should not be granted to generated workflow agents."""
    allowed = sorted(
        name
        for name, metadata in TOOL_METADATA.items()
        if metadata.available_to_generated_agents
        and name not in NEVER_PREAPPROVE_TOOLKITS
        and not metadata.requires_external_credentials
    )
    rejected = sorted(
        name
        for name in TOOL_METADATA
        if name not in allowed
    )
    assert allowed, "at least one generated-agent-safe tool is required for this safety regression test"
    assert rejected, "at least one restricted tool is required for this safety regression test"

    for tool_name in rejected:
        spec = _workflow_spec(
            participants=[
                {
                    "id": "writer",
                    "kind": "ephemeral_agent",
                    "name": "Report Writer",
                    "tools": [tool_name],
                },
            ],
            permissions={**_workflow_spec()["permissions"], "tools": [tool_name]},
        )
        with pytest.raises(DynamicWorkflowError, match="not allowed for generated workflow agents"):
            dynamic_workflow_module._validate_workflow_tool_policy(spec)


def test_validate_workflow_spec_rejects_room_agent_participant_tools() -> None:
    """Room-agent participants should never receive extra workflow-declared tools."""
    spec = _workflow_spec(
        participants=[
            {"id": "reviewer", "kind": "room_agent", "agent": "reviewer", "tools": ["memory"]},
        ],
        permissions={**_workflow_spec()["permissions"], "tools": ["memory"]},
    )

    with pytest.raises(DynamicWorkflowError, match="must not declare tools"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unimplemented_thread_data_permissions() -> None:
    """Workflow data permissions should fail closed on unsupported thread-history access."""
    spec = _workflow_spec(permissions={**_workflow_spec()["permissions"], "data": {"matrix_history": "thread"}})

    with pytest.raises(DynamicWorkflowError, match="permissions.data.matrix_history supports only"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unimplemented_knowledge_data_permissions() -> None:
    """Workflow data permissions should reject unsupported knowledge access before execution."""
    spec = _workflow_spec(
        permissions={**_workflow_spec()["permissions"], "data": {"knowledge_bases": ["reference"]}},
    )

    with pytest.raises(DynamicWorkflowError, match="permissions.data.knowledge_bases is not available"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unknown_data_permissions() -> None:
    """Workflow specs should fail closed on unknown data permission blocks."""
    spec = _workflow_spec(permissions={**_workflow_spec()["permissions"], "data": {"secrets": "all"}})

    with pytest.raises(DynamicWorkflowError, match="permissions.data contains unsupported key 'secrets'"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unsupported_participant_fields() -> None:
    """Participants should fail closed on unsupported capability fields."""
    spec = _workflow_spec(participants=[{"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer", "temperature": 1}])

    with pytest.raises(DynamicWorkflowError, match="participant\[0\] contains unsupported key 'temperature'"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unsupported_agent_step_fields() -> None:
    """Agent steps should fail closed on unsupported capability fields."""
    spec = _workflow_spec(workflow=[{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write.", "tools": ["memory"]}])

    with pytest.raises(DynamicWorkflowError, match="workflow\[0\] contains unsupported key 'tools'"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_ambiguous_report_step_source() -> None:
    """Report outputs should require exactly one source field."""
    spec = _workflow_spec(
        outputs=[{"id": "report_html", "type": "html_report", "from_step": "write", "from_participant": "writer"}],
    )

    with pytest.raises(DynamicWorkflowError, match="must declare exactly one source"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_output_without_source_step() -> None:
    """Outputs must declare a step or participant source."""
    spec = _workflow_spec(outputs=[{"id": "report_html", "type": "html_report"}])

    with pytest.raises(DynamicWorkflowError, match="must declare exactly one source"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_output_without_type() -> None:
    """Outputs must declare a supported output type."""
    spec = _workflow_spec(outputs=[{"id": "report_html", "from_step": "write"}])

    with pytest.raises(DynamicWorkflowError, match="outputs\[0\].type"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unsupported_output_type() -> None:
    """Outputs should accept only supported output types."""
    spec = _workflow_spec(outputs=[{"id": "report_html", "type": "video", "from_step": "write"}])

    with pytest.raises(DynamicWorkflowError, match="outputs\[0\].type"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_run_workflow_executes_steps_and_persists_outputs(tmp_path: Path) -> None:
    """Agent steps should execute through the provided participant executor."""
    prompts: list[str] = []

    async def participant_executor(participant: dict[str, object], prompt: str) -> str:
        prompts.append(f"{participant['id']}:{prompt}")
        return f"Generated report for prompt: {prompt}"

    spec = _workflow_spec(
        participants=[
            {"id": "researcher", "kind": "ephemeral_agent", "name": "Researcher", "model": "claude-sonnet-4-6", "tools": []},
            {"id": "writer", "kind": "ephemeral_agent", "name": "Writer", "model": "claude-sonnet-4-6", "tools": []},
        ],
        workflow=[
            {"id": "research", "type": "agent_step", "participant": "researcher", "prompt": "Find competitors for {input.topic}."},
            {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write report from {steps.research}."},
            {"id": "format", "type": "transform_step", "template": "# Report\n{steps.write}"},
        ],
        outputs=[
            {"id": "brief", "type": "text", "from_step": "research"},
            {"id": "report_html", "type": "html_report", "from_step": "format"},
        ],
    )
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store, participant_executor=participant_executor)

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "MindRoom"},
        requested_by="@user:localhost",
        base_url="https://mindroom.test",
    )

    assert prompts == [
        "researcher:Find competitors for MindRoom.",
        "writer:Write report from Generated report for prompt: Find competitors for MindRoom..",
    ]
    assert run.status == "completed"
    assert run.outputs["brief"] == "Generated report for prompt: Find competitors for MindRoom."
    assert run.outputs["report_html"].startswith("# Report\nGenerated report")
    loaded = store.get_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded.outputs == run.outputs
    assert loaded.artifacts["report_html"].endswith(".html")


def test_validate_workflow_spec_rejects_missing_step_id() -> None:
    """Workflow steps must have explicit IDs."""
    spec = _workflow_spec(workflow=[{"type": "agent_step", "participant": "writer", "prompt": "Write."}])

    with pytest.raises(DynamicWorkflowError, match="workflow\[0\].id"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_ambiguous_agent_step_template() -> None:
    """Agent steps should use prompt, not template fields."""
    spec = _workflow_spec(workflow=[{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write.", "template": "Also write."}])

    with pytest.raises(DynamicWorkflowError, match="must not include template"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_validate_workflow_spec_rejects_unsupported_participant_kind() -> None:
    """Participants should be either ephemeral_agent or room_agent."""
    spec = _workflow_spec(participants=[{"id": "writer", "kind": "automation"}])

    with pytest.raises(DynamicWorkflowError, match="participant\[0\].kind"):
        DynamicWorkflowStore.validate_workflow_spec(spec)


def test_get_workflow_run_rejects_traversal_run_id(tmp_path: Path) -> None:
    """Run IDs should not traverse outside the workflow private store."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(workflow=[{"id": "research", "type": "transform_step", "template": "brief"}])
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")

    with pytest.raises(DynamicWorkflowError, match="Invalid run id"):
        store.get_run(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            run_id="../secret",
        )


def test_load_workflow_revision_rejects_traversal_revision(tmp_path: Path) -> None:
    """Revision IDs should not traverse outside the workflow private store."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(workflow=[{"id": "research", "type": "transform_step", "template": "brief"}])
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")

    with pytest.raises(DynamicWorkflowError, match="Invalid revision"):
        store.get_revision(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            revision="../../old",
        )


def test_get_workflow_run_wraps_json_decoder_errors(tmp_path: Path) -> None:
    """Corrupted run metadata should raise a stable DynamicWorkflowError."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(workflow=[{"id": "research", "type": "transform_step", "template": "brief"}])
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    bad_run_dir = (
        tmp_path
        / "mindroom_data"
        / "dynamic_workflows"
        / "agent"
        / "general"
        / "competitor-research-report"
        / "runs"
        / "run_bad"
    )
    bad_run_dir.mkdir(parents=True)
    (bad_run_dir / "run.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(DynamicWorkflowError, match="Failed to load Dynamic Workflow run"):
        store.get_run(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            run_id="run_bad",
        )


def test_run_workflow_records_failed_run_when_stored_step_reference_is_missing(tmp_path: Path) -> None:
    """Executor failures after a run id is allocated should persist failed run metadata."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(
        workflow=[{"id": "research", "type": "transform_step", "template": "Research brief for {input.topic}."}],
        outputs=[{"id": "brief", "type": "text", "from_step": "missing"}],
    )
    store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    service = DynamicWorkflowService(store)

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@user:localhost",
        base_url="https://mindroom.test",
    )

    assert run.status == "failed"
    assert run.error == "Output 'brief' references unknown step 'missing'."
    loaded_run = store.get_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded_run.status == "failed"
    assert loaded_run.error == "Output 'brief' references unknown step 'missing'."


def test_run_workflow_records_failed_run_when_active_revision_is_missing(tmp_path: Path) -> None:
    """Missing active revision metadata should persist a failed run record."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    spec = _workflow_spec(workflow=[{"id": "research", "type": "transform_step", "template": "Research brief for {input.topic}."}])
    summary = store.create_workflow(spec=spec, scope="agent", owner_id="general", created_by="general")
    revision_path = (
        tmp_path
        / "mindroom_data"
        / "dynamic_workflows"
        / "agent"
        / "general"
        / "competitor-research-report"
        / "revisions"
        / f"{summary.active_revision}.json"
    )
    revision_path.unlink()
    service = DynamicWorkflowService(store)

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@user:localhost",
        base_url="https://mindroom.test",
    )

    assert run.status == "failed"
    assert "Active Dynamic Workflow revision" in str(run.error)
    loaded_run = store.get_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded_run.status == "failed"
    assert "Active Dynamic Workflow revision" in str(loaded_run.error)


def test_declarative_spec_compiles_to_agno_workflow_factory() -> None:
    """Workflow specs should compile into Agno WorkflowFactory definitions."""
    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer"}],
        workflow=[{"id": "write", "type": "transform_step", "template": "# {input.topic}"}],
    )

    factory = build_agno_workflow_factory(spec, db_file="dynamic-workflow.db")

    assert isinstance(factory, WorkflowFactory)
    resolved = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno"}), Workflow)
    assert isinstance(resolved, Workflow)
    assert resolved.workflow_id == "competitor_research_report_workflow"
    assert resolved.description == "Create a cited HTML report about competitors."
    assert resolved.db_file == "dynamic-workflow.db"


def test_agno_workflow_factory_step_executor_renders_declared_output() -> None:
    """WorkflowFactory should expose an executable step callback for transform workflows."""
    spec = _workflow_spec(workflow=[{"id": "write", "type": "transform_step", "template": "# {input.topic}"}])
    factory = build_agno_workflow_factory(spec, db_file="dynamic-workflow.db")
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno"}), Workflow)

    output = workflow._step_executor(StepInput(input={"topic": "Agno"}, previous_step_content=None, additional_data={}))

    assert isinstance(output, StepOutput)
    assert output.content == "# Agno"
    assert output.success is True


def test_agno_workflow_factory_step_executor_runs_participant() -> None:
    """Agent steps should call the injected participant executor with rendered prompt content."""
    prompts: list[str] = []

    async def participant_executor(participant: dict[str, object], prompt: str) -> str:
        prompts.append(f"{participant['id']}:{prompt}")
        return f"Generated: {prompt}"

    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer"}],
        workflow=[{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write about {input.topic}."}],
    )
    factory = build_agno_workflow_factory(spec, db_file="dynamic-workflow.db", participant_executor=participant_executor)
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno"}), Workflow)

    output = workflow._step_executor(StepInput(input={"topic": "Agno"}, previous_step_content=None, additional_data={}))

    assert prompts == ["writer:Write about Agno."]
    assert output.content == "Generated: Write about Agno."
    assert output.success is True


def test_agno_workflow_run_fails_and_stops_when_step_execution_fails() -> None:
    """Agno Workflow.run should surface failed StepOutput as DynamicWorkflowExecutionError."""

    async def participant_executor(_participant: dict[str, object], _prompt: str) -> str:
        raise DynamicWorkflowExecutionError("provider auth failed")

    spec = _workflow_spec(
        participants=[{"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer"}],
        workflow=[
            {
                "id": "write",
                "type": "agent_step",
                "participant": "writer",
                "prompt": "Write about {input.topic}.",
            },
            {
                "id": "after",
                "type": "transform_step",
                "template": "Should not run for {input.topic}.",
            },
        ],
        outputs=[{"id": "result", "type": "text", "from_step": "after"}],
    )
    factory = build_agno_workflow_factory(
        spec,
        db_file=tmp_path / "dynamic-workflow-agno.db",
        participant_executor=participant_executor,
    )
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    with pytest.raises(DynamicWorkflowExecutionError, match="provider auth failed"):
        workflow.run(input={"topic": "Agno factories"}, user_id="@user:localhost")

    assert prompts == ["Write about Agno factories."]


def test_dynamic_workflow_tool_uses_runtime_context(tmp_path: Path) -> None:
    """Runtime-aware tool should scope workflows to current agent and storage root."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )

    with tool_runtime_context(context):
        created = _tool_payload(tool.create_workflow(transform_spec, reason="initial design"))
        listed = _tool_payload(tool.list_workflows())
        run = _tool_payload(
            tool.run_workflow(
                workflow_id="competitor-research-report",
                input={"topic": "Agno factories"},
            ),
        )

    assert created["status"] == "ok"
    assert created["workflow_id"] == "competitor-research-report"
    assert listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert run["status"] == "completed"
    assert run["outputs"]["brief"] == "Research brief for Agno factories."
    assert run["report_url"].startswith(
        "https://acme.mindroom.chat/reports/private/agent/general/competitor-research-report/run_",
    )


def test_dynamic_workflow_tool_denies_run_read_for_different_requester(tmp_path: Path) -> None:
    """Agent-scoped run details should not leak across Matrix requesters."""
    tool = DynamicWorkflowTools()
    alice_context = _make_context(tmp_path)
    bob_context = replace(alice_context, requester_id="@bob:localhost")
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )

    with tool_runtime_context(alice_context):
        _tool_payload(tool.create_workflow(transform_spec, reason="initial design"))
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))
    with tool_runtime_context(bob_context):
        result = _tool_payload(tool.get_workflow_run("competitor-research-report", run["run_id"]))

    assert result["status"] == "error"
    assert "not available to the current requester" in result["message"]


def test_dynamic_workflow_tool_json_schemas_allow_arbitrary_json_values() -> None:
    """Tool parameter schemas should not reject valid object-shaped workflow specs or inputs."""
    toolkit = DynamicWorkflowTools()
    create_schema = toolkit.functions["create_workflow"].parameters
    run_schema = toolkit.functions["run_workflow"].parameters

    assert create_schema["properties"]["spec"]["additionalProperties"] is True
    assert run_schema["properties"]["input"]["additionalProperties"] is True


def test_dynamic_workflow_tool_scopes_private_agent_workflows_by_requester(tmp_path: Path) -> None:
    """Private agents should not share Dynamic Workflows across different requester accounts."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    private=AgentPrivateConfig(workspace_enabled=True),
                    tools=["dynamic_workflow"],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    alice_context = replace(context, config=config, runtime_paths=runtime_paths, requester_id="@alice:localhost")
    bob_context = replace(context, config=config, runtime_paths=runtime_paths, requester_id="@bob:localhost")

    with tool_runtime_context(alice_context):
        created = _tool_payload(tool.create_workflow(_workflow_spec(), reason="alice workflow"))
        alice_workflows = _tool_payload(tool.list_workflows())
    with tool_runtime_context(bob_context):
        bob_workflows = _tool_payload(tool.list_workflows())

    assert created["status"] == "ok"
    assert alice_workflows["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert bob_workflows["workflows"] == []
    assert alice_workflows["owner_id"] != bob_workflows["owner_id"]


def test_dynamic_workflow_tool_rejects_ephemeral_model_outside_caller_policy(tmp_path: Path) -> None:
    """Generated workflow agents should inherit the caller's model allowlist."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec(
        participants=[
            {"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer", "model": "claude-opus-5", "tools": []},
        ],
        permissions={**_workflow_spec()["permissions"], "models": ["claude-opus-5"]},
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(spec, reason="needs strong model"))

    assert result["status"] == "error"
    assert "not allowed for agent 'general'" in result["message"]


def test_dynamic_workflow_tool_enforces_permission_models_for_default_participant_model(tmp_path: Path) -> None:
    """A defaulted ephemeral participant model must still be granted by permissions.models."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "tools": [],
            },
        ],
        permissions={**_workflow_spec()["permissions"], "models": ["claude-opus-5"]},
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(spec, reason="missing model grant"))

    assert result["status"] == "error"
    assert "not granted by permissions.models" in result["message"]


def test_dynamic_workflow_tool_defaults_ephemeral_model_to_caller_runtime_model(tmp_path: Path) -> None:
    """Ephemeral participants without model should run on the caller agent's runtime model."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec(
        participants=[
            {"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer", "tools": []},
        ],
        permissions={**_workflow_spec()["permissions"], "models": ["claude-sonnet-4-6"]},
    )

    with tool_runtime_context(context):
        created = _tool_payload(tool.create_workflow(spec, reason="default model"))
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))

    assert created["status"] == "ok"
    assert run["status"] == "completed"


def test_dynamic_workflow_tool_rejects_unknown_room_agent_during_validation(tmp_path: Path) -> None:
    """Workflow validation should reject undeclared room-agent participants."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec(
        participants=[{"id": "reviewer", "kind": "room_agent", "agent": "reviewer"}],
        workflow=[{"id": "review", "type": "agent_step", "participant": "reviewer", "prompt": "Review."}],
        outputs=[{"id": "review", "type": "text", "from_step": "review"}],
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(spec, reason="needs reviewer"))

    assert result["status"] == "error"
    assert "not found in config" in result["message"]


def test_dynamic_workflow_tool_rejects_unavailable_room_agent_during_validation(tmp_path: Path) -> None:
    """Workflow validation should require room-agent participants to be available in-room."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])
    spec = _workflow_spec(
        participants=[{"id": "specialist", "kind": "room_agent", "agent": "specialist"}],
        workflow=[{"id": "review", "type": "agent_step", "participant": "specialist", "prompt": "Review."}],
        outputs=[{"id": "review", "type": "text", "from_step": "review"}],
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(spec, reason="needs specialist"))

    assert result["status"] == "error"
    assert "not available to this requester in this room" in result["message"]


def test_dynamic_workflow_validation_uses_current_authorization_after_reload(tmp_path: Path) -> None:
    """Policy checks should not rely on stale config objects after runtime reload."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    allowed_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
            authorization=AuthorizationConfig(
                agents={
                    "general": AgentReplyPermission(model_allowlist=["claude-sonnet-4-6", "claude-opus-5"]),
                },
            ),
        ),
        context.runtime_paths,
    )
    denied_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
        ),
        context.runtime_paths,
    )
    spec = _workflow_spec(
        participants=[
            {"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer", "model": "claude-opus-5", "tools": []},
        ],
        permissions={**_workflow_spec()["permissions"], "models": ["claude-opus-5"]},
    )

    with tool_runtime_context(replace(context, config=allowed_config, runtime_paths=runtime_paths_for(allowed_config))):
        allowed = _tool_payload(tool.create_workflow(spec, reason="allowed"))
    with tool_runtime_context(replace(context, config=denied_config, runtime_paths=runtime_paths_for(denied_config))):
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))

    assert allowed["status"] == "ok"
    assert run["status"] == "failed"
    assert "not allowed for agent 'general'" in str(run["error"])


def test_dynamic_workflow_tool_uses_cached_client_room_when_context_room_is_missing(tmp_path: Path) -> None:
    """Room-agent availability validation should fall back to the client's cached room object."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    context = replace(context, room=None)
    context.client.rooms = {"!room:localhost": context.room}
    spec = _workflow_spec(
        participants=[{"id": "specialist", "kind": "room_agent", "agent": "specialist"}],
        workflow=[{"id": "review", "type": "agent_step", "participant": "specialist", "prompt": "Review."}],
        outputs=[{"id": "review", "type": "text", "from_step": "review"}],
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(spec, reason="needs specialist"))

    assert result["status"] == "ok"


def test_dynamic_workflow_tool_revalidates_saved_revision_policy_before_run(tmp_path: Path) -> None:
    """Running a stored workflow should re-check current model/tool policy."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    spec = _workflow_spec(
        participants=[
            {"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer", "model": "claude-opus-5", "tools": []},
        ],
        permissions={**_workflow_spec()["permissions"], "models": ["claude-opus-5"]},
    )
    allowed_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
            authorization=AuthorizationConfig(
                agents={"general": AgentReplyPermission(model_allowlist=["claude-sonnet-4-6", "claude-opus-5"])},
            ),
        ),
        context.runtime_paths,
    )
    denied_config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
        ),
        context.runtime_paths,
    )

    with tool_runtime_context(replace(context, config=allowed_config, runtime_paths=runtime_paths_for(allowed_config))):
        created = _tool_payload(tool.create_workflow(spec, reason="allowed"))
    with tool_runtime_context(replace(context, config=denied_config, runtime_paths=runtime_paths_for(denied_config))):
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))

    assert created["status"] == "ok"
    assert run["status"] == "failed"
    assert "not allowed for agent 'general'" in str(run["error"])


def test_dynamic_workflow_tool_returns_payload_for_invalid_scope(tmp_path: Path) -> None:
    """Tool calls should return JSON payload errors instead of raising runtime exceptions."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows(scope="global"))

    assert result["status"] == "error"
    assert "Unsupported Dynamic Workflow scope" in result["message"]


def test_dynamic_workflow_tool_returns_payload_when_agent_name_is_missing(tmp_path: Path) -> None:
    """Runtime-aware tool should fail cleanly when required context owner data is missing."""
    tool = DynamicWorkflowTools()
    context = replace(_make_context(tmp_path), agent_name="")

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows())

    assert result["status"] == "error"
    assert "Agent name is missing" in result["message"]



def _write_dynamic_workflow_promotion(context: ToolRuntimeContext, spec: dict[str, object], *, scope: str = "room") -> None:
    spec_path = context.runtime_paths.storage_root / "dynamic-workflow-promotion-spec.json"
    raw = json.dumps(spec, sort_keys=True).encode("utf-8")
    spec_path.write_bytes(raw)
    promotions_dir = context.runtime_paths.storage_root / "plugins" / "dynamic-workflow-promotion" / "promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)
    promotion = {
        "schema_version": 1,
        "status": "active",
        "workflow_id": str(spec["id"]),
        "workflow_spec_ref": str(spec_path),
        "spec_hash": __import__("hashlib").sha256(raw).hexdigest(),
        "target_scope": scope,
        "target_ref": context.room_id if scope == "room" else "tenant",
        "created_by": "dynamic_workflow_promotion",
        "reason": "approved promotion",
    }
    (promotions_dir / "promotion.json").write_text(json.dumps(promotion), encoding="utf-8")


def test_room_scope_consumes_active_dynamic_workflow_promotion(tmp_path: Path) -> None:
    """Room read/run paths should consume active audited promotion records."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Promoted brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )
    _write_dynamic_workflow_promotion(context, transform_spec)

    with tool_runtime_context(context):
        listed = _tool_payload(tool.list_workflows(scope="room"))
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}, scope="room"))

    assert listed["status"] == "ok"
    assert listed["owner_id"] == "!room:localhost"
    assert listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert run["status"] == "completed"
    assert run["outputs"]["brief"] == "Promoted brief for Agno factories."
    assert run["report_url"].startswith(
        "https://acme.mindroom.chat/reports/private/room/!room:localhost/competitor-research-report/run_",
    )


def test_room_scope_rejects_hash_mismatched_dynamic_workflow_promotion(tmp_path: Path) -> None:
    """Promoted workflow specs should fail closed when the audited hash no longer matches."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Promoted brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )
    _write_dynamic_workflow_promotion(context, transform_spec)
    (context.runtime_paths.storage_root / "dynamic-workflow-promotion-spec.json").write_text(
        json.dumps(_workflow_spec(name="Tampered Workflow")),
        encoding="utf-8",
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows(scope="room"))

    assert result["status"] == "error"
    assert "hash mismatch" in result["message"]
def test_dynamic_workflow_tool_denies_shared_scopes_without_policy(tmp_path: Path) -> None:
    """Agent tools should not mutate room or tenant workflow scopes without an approval policy."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        room_result = _tool_payload(tool.create_workflow(_workflow_spec(), scope="room"))
        tenant_result = _tool_payload(tool.create_workflow(_workflow_spec(), scope="tenant"))

    assert room_result["status"] == "error"
    assert "scope requires Dynamic Workflow approval policy" in room_result["message"]
    assert tenant_result["status"] == "error"
    assert "scope requires Dynamic Workflow approval policy" in tenant_result["message"]


def test_room_agent_participant_must_be_available_to_requester_in_room(tmp_path: Path) -> None:
    """Room-agent participants should not bypass normal room responder eligibility."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])

    with pytest.raises(DynamicWorkflowError, match="not available to this requester in this room"):
        dynamic_workflow_module._execute_room_agent_participant(
            context,
            {"id": "specialist", "kind": "room_agent", "agent": "specialist"},
            "Write a report.",
        )


def test_room_agent_participant_rejects_model_override(tmp_path: Path) -> None:
    """Room-agent participants should run with their configured model only."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])

    with pytest.raises(DynamicWorkflowError, match="configured model"):
        dynamic_workflow_module._execute_room_agent_participant(
            context,
            {"id": "specialist", "kind": "room_agent", "agent": "specialist", "model": "default"},
            "Write a report.",
        )


def test_room_agent_participant_rebinds_context_and_uses_isolated_state(tmp_path: Path) -> None:
    """Room-agent participants should execute as that agent without durable workflow side effects."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"]),
                "specialist": AgentConfig(
                    display_name="Specialist Agent",
                    model="default",
                    tools=["memory"],
                    knowledge_bases=["reference"],
                ),
            },
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
            room_models={"lobby": "large"},
            knowledge_bases={"reference": {"path": str(tmp_path / "knowledge")}},
        ),
        context.runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("lobby", room_id="!room:localhost", alias="#lobby:localhost", name="Lobby")
    state.save(runtime_paths=runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    context = replace(context, config=config, runtime_paths=runtime_paths)
    parent_loop = asyncio.new_event_loop()

    def assert_run(prompt: str, *, user_id: str, session_id: str) -> None:
        runtime_context = get_tool_runtime_context()
        assert runtime_context is not None
        assert runtime_context.agent_name == "specialist"
        assert runtime_context.storage_path == runtime_paths.storage_root / "agents" / "specialist"
        assert runtime_context.relations.requester_id == "@user:localhost"
        assert runtime_context.room_alias == "#lobby:localhost"
        assert runtime_context.room_display_name == "Lobby"
        assert runtime_context.config.agents["specialist"].tools == ["memory"]
        assert runtime_context.config.agents["specialist"].knowledge_bases == ["reference"]
        assert runtime_context.config.agents["specialist"].model == "default"
        assert runtime_context.runtime_paths == runtime_paths
        assert prompt == "Write a report."
        assert user_id == "@user:localhost"
        assert session_id.startswith("dynamic_workflow:!room:localhost:$thread:localhost")
        assert asyncio.get_running_loop() is not parent_loop

    with tool_runtime_context(context):
        result = parent_loop.run_until_complete(
            dynamic_workflow_module._execute_room_agent_participant(
                context,
                {"id": "specialist", "kind": "room_agent", "agent": "specialist"},
                "Write a report.",
                agent_factory=lambda _name, _config, _runtime_paths: _fake_stream_agent(
                    content="Specialist report.",
                    on_run=assert_run,
                ),
            ),
        )

    assert result == "Specialist report."


def test_dynamic_workflow_uses_shared_automation_approval_policy(tmp_path: Path) -> None:
    """Dynamic Workflow tool policy should match the central automation pre-approval builder."""
    toolkit = DynamicWorkflowTools()
    dynamic_workflow_function = toolkit.functions["create_workflow"]
    config = Config(
        automation_approval={
            "restricted_toolkits": ["dynamic_workflow"],
            "toolkit_overrides": {"dynamic_workflow": {"mode": "always_require"}},
        },
    )

    rule = _matching_tool_approval_rule(
        tool_name=dynamic_workflow_function.name,
        toolkit=dynamic_workflow_function,
        rules=build_automation_approval_config(config).rules,
    )

    assert rule is not None
    assert rule.mode == "always_require"


def test_dynamic_workflow_tool_exposes_agno_function_schemas() -> None:
    """Agno Function metadata should expose strict, documentation-rich schemas."""
    toolkit = DynamicWorkflowTools()
    for name in (
        "create_workflow",
        "validate_workflow",
        "update_workflow",
        "run_workflow",
        "get_workflow_run",
        "list_workflows",
        "list_workflow_revisions",
    ):
        assert isinstance(toolkit.functions[name], Function)

    create_workflow = toolkit.functions["create_workflow"]
    assert create_workflow.name == "create_workflow"
    assert create_workflow.description
    assert create_workflow.entrypoint is not None
    spec_schema = create_workflow.parameters["properties"]["spec"]
    assert spec_schema["type"] == "object"
    assert spec_schema["description"].startswith("Declarative Dynamic Workflow spec")
    assert spec_schema["additionalProperties"] is True
    assert "schema_version" in spec_schema["properties"]

    run_workflow = toolkit.functions["run_workflow"]
    assert run_workflow.parameters["properties"]["input"]["additionalProperties"] is True


def test_dynamic_workflow_tool_injects_minimal_spec_example() -> None:
    """Tool descriptions should include a compact valid example for generated agents."""
    toolkit = DynamicWorkflowTools()
    create_description = toolkit.functions["create_workflow"].description or ""
    validate_description = toolkit.functions["validate_workflow"].description or ""

    assert _MINIMAL_SPEC_EXAMPLE in create_description
    assert _MINIMAL_SPEC_EXAMPLE in validate_description


def test_dynamic_workflow_request_guidance_includes_review_safety() -> None:
    """Natural-language guidance should steer agents toward reviewable workflow specs."""
    description = TOOL_METADATA["dynamic_workflow"].request_guidance or ""

    assert "Review the current request" in description
    assert "validation findings" in description
    assert "before running" in description


def test_validate_workflow_reports_all_spec_errors_in_one_call() -> None:
    """Validation should return aggregated spec errors instead of stopping at the first one."""
    tool = DynamicWorkflowTools()
    invalid_spec = {"schema_version": 2, "id": "Bad Workflow", "kind": "workflow", "inputs": {"type": []}}

    result = _tool_payload(tool.validate_workflow(invalid_spec))

    assert result["status"] == "error"
    assert result["message"] == "Dynamic Workflow spec validation failed."
    assert result["errors"] == [
        "schema_version must be 1.",
        "id must be lowercase kebab-case with letters, numbers, and hyphens.",
        "name must be a non-empty string.",
        "description must be a non-empty string.",
        "inputs.type must not be empty.",
        "workflow must be a non-empty list.",
        "outputs must be a non-empty list.",
    ]


def test_create_workflow_reports_all_spec_errors_in_one_call(tmp_path: Path) -> None:
    """Create should surface all schema-level errors before deeper policy checks."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    invalid_spec = {"schema_version": 2, "id": "Bad Workflow", "kind": "workflow", "inputs": {"type": []}}

    with tool_runtime_context(context):
        result = _tool_payload(tool.create_workflow(invalid_spec))

    assert result["status"] == "error"
    assert result["message"] == "Dynamic Workflow spec validation failed."
    assert result["errors"] == [
        "schema_version must be 1.",
        "id must be lowercase kebab-case with letters, numbers, and hyphens.",
        "name must be a non-empty string.",
        "description must be a non-empty string.",
        "inputs.type must not be empty.",
        "workflow must be a non-empty list.",
        "outputs must be a non-empty list.",
    ]