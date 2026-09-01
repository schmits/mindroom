"""Logical-child observed-agent scoping tests for session-completion-notifier."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.hooks import AfterResponseContext, MessageEnvelope, ResponseResult, ToolAfterCallContext
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import classify_turn_origin

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_hooks_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_completion_notifier_hooks_logical_scope", PLUGIN_ROOT / "hooks.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )


def _config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        Config(agents={"mind": AgentConfig(display_name="Mind", rooms=["!room:localhost"])}).authored_model_dump(),
        runtime_paths,
    )
    return config, runtime_paths


def _origin():
    return classify_turn_origin(
        transport_sender_id="@user:localhost",
        requester_id="@user:localhost",
        sender_entity_name=None,
        requester_entity_name=None,
        source_kind=MESSAGE_SOURCE_KIND,
        original_sender=None,
        trusted_user_relay=False,
    )


def _after_context(tmp_path: Path, *, agent_name: str = "mind") -> AfterResponseContext:
    config, runtime_paths = _config(tmp_path)
    envelope = MessageEnvelope(
        source_event_id="$source",
        target=MessageTarget.resolve("!room:localhost", "$thread", "$source"),
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        origin=_origin(),
    )
    return AfterResponseContext(
        event_name="message:after_response",
        plugin_name="session-completion-notifier",
        settings={"agents": ["workflow_builder"], "notify_room_id": "!ops:localhost", "wake_bridge_enabled": True},
        config=config,
        runtime_paths=runtime_paths,
        logger=get_logger("tests.session_completion_notifier.logical_scope"),
        correlation_id="corr-1",
        message_sender=AsyncMock(return_value="$notice"),
        room_state_querier=AsyncMock(return_value=None),
        room_state_putter=AsyncMock(return_value=True),
        result=ResponseResult(
            response_text="secret response text",
            response_event_id="$response",
            delivery_kind="sent",
            response_kind="ai",
            envelope=envelope,
        ),
    )


def _tool_context(tmp_path: Path, settings: dict[str, object]) -> ToolAfterCallContext:
    config, runtime_paths = _config(tmp_path)
    return ToolAfterCallContext(
        event_name="tool:after_call",
        plugin_name="session-completion-notifier",
        settings=settings,
        config=config,
        runtime_paths=runtime_paths,
        logger=get_logger("tests.session_completion_notifier.logical_scope"),
        correlation_id="corr-tool",
        tool_name="sessions_spawn",
        arguments={},
        agent_name="mind",
        room_id="!parent:localhost",
        thread_id="$parent-thread",
        requester_id="@user:localhost",
        session_id="parent-session",
        result=None,
        error=None,
        blocked=False,
        duration_ms=1.0,
    )


async def _record_mapping(hooks: ModuleType, ctx: AfterResponseContext, tmp_path: Path, logical_agent: str) -> None:
    await hooks._record_parent_session_mapping(
        _tool_context(tmp_path, ctx.settings),
        ctx.result.envelope.target.session_id,
        logical_agent,
    )


@pytest.mark.asyncio
async def test_mapped_logical_child_in_allowlist_wakes_for_physical_mind(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path)
    await _record_mapping(hooks, ctx, tmp_path, "workflow_builder")

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    payload = ctx.message_sender.await_args.args[4]["mindroom.session_completion"]
    assert payload["agent"] == "workflow_builder"
    assert payload["physical_agent"] == "mind"
    assert payload["logical_agent"] == "workflow_builder"
    assert payload["observed_agent_trust"] == "mapped"
    assert "response_text" not in json.dumps(payload, sort_keys=True)


@pytest.mark.asyncio
async def test_true_mind_without_logical_mapping_is_suppressed(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path)

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert not (ctx.state_root / "dedupe.json").exists()


@pytest.mark.asyncio
async def test_mapped_unallowlisted_logical_child_is_suppressed(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path)
    await _record_mapping(hooks, ctx, tmp_path, "agent_builder")

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert not (ctx.state_root / "dedupe.json").exists()


@pytest.mark.asyncio
async def test_direct_observed_child_agent_still_matches_allowlist(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, agent_name="workflow_builder")

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    payload = ctx.message_sender.await_args.args[4]["mindroom.session_completion"]
    assert payload["agent"] == "workflow_builder"
    assert payload["physical_agent"] == "workflow_builder"
    assert payload["logical_agent"] is None
    assert payload["observed_agent_trust"] == "direct"
    assert "response_text" not in json.dumps(payload, sort_keys=True)