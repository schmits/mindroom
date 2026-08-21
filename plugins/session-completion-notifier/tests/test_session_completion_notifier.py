"""Tests for the session completion notifier plugin."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.hooks import AfterResponseContext, MessageEnvelope, ResponseResult
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import classify_turn_origin

if TYPE_CHECKING:
    from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_hooks_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_completion_notifier_hooks", PLUGIN_ROOT / "hooks.py")
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


def _envelope() -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$source",
        target=MessageTarget.resolve("!room:localhost", "$thread", "$source"),
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="mind",
        origin=classify_turn_origin(
            transport_sender_id="@user:localhost",
            requester_id="@user:localhost",
            sender_entity_name=None,
            requester_entity_name=None,
            source_kind=MESSAGE_SOURCE_KIND,
            original_sender=None,
            trusted_user_relay=False,
        ),
    )


def _after_context(tmp_path: Path, *, settings: dict[str, object] | None = None) -> AfterResponseContext:
    config, runtime_paths = _config(tmp_path)
    return AfterResponseContext(
        event_name="message:after_response",
        plugin_name="session-completion-notifier",
        settings=settings or {},
        config=config,
        runtime_paths=runtime_paths,
        logger=get_logger("tests.session_completion_notifier"),
        correlation_id="corr-1",
        message_sender=AsyncMock(return_value="$notice"),
        room_state_querier=AsyncMock(return_value=None),
        room_state_putter=AsyncMock(return_value=True),
        result=ResponseResult(
            response_text="secret response text",
            response_event_id="$response",
            delivery_kind="sent",
            response_kind="ai",
            envelope=_envelope(),
        ),
    )


def _sent_body(ctx: AfterResponseContext) -> str:
    return ctx.message_sender.await_args.args[1]


@pytest.mark.asyncio
async def test_no_configured_destination_writes_passive_ledger_without_wake_notification(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"parent_ledger_enabled": True})

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert (ctx.state_root / "parent_ledger.json").is_file()


@pytest.mark.asyncio
async def test_explicit_wake_bridge_false_keeps_legacy_passive_json_notification(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"notify_room_id": "!ops:localhost", "wake_bridge_enabled": False})

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!ops:localhost"
    assert call.kwargs["trigger_dispatch"] is False
    assert _sent_body(ctx).startswith("{")
    assert "@mindroom_mind_mm3j9z5u:mindroom.chat" not in _sent_body(ctx)
    assert "secret response text" not in _sent_body(ctx)
    assert "response_text" not in _sent_body(ctx)


@pytest.mark.asyncio
async def test_notify_room_without_wake_intent_keeps_legacy_passive_json(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"notify_room_id": "!ops:localhost"})

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_args.kwargs["trigger_dispatch"] is False
    assert _sent_body(ctx).startswith("{")


@pytest.mark.asyncio
async def test_parent_ledger_destination_is_existing_intent_for_minimized_wake_bridge(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_room_id": "!parent:localhost"},
    )

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!parent:localhost"
    assert call.args[2] is None
    assert call.kwargs["trigger_dispatch"] is True
    assert _sent_body(ctx).startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    assert '"correlation_id":"corr-1"' in _sent_body(ctx)
    assert '"source_event_id":"$source"' in _sent_body(ctx)
    assert '"response_event_id":"$response"' in _sent_body(ctx)
    assert "secret response text" not in _sent_body(ctx)
    assert "response_text" not in _sent_body(ctx)
    content = ctx.room_state_putter.await_args.args[3]
    assert "response_text" not in json.dumps(content, sort_keys=True)


@pytest.mark.asyncio
async def test_notify_destination_plus_parent_ledger_enabled_wakes_mind(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"parent_ledger_enabled": True, "notify_room_id": "!ops:localhost"})

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_args.args[0] == "!ops:localhost"
    assert ctx.message_sender.await_args.kwargs["trigger_dispatch"] is True
    assert _sent_body(ctx).startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    assert "secret response text" not in _sent_body(ctx)


@pytest.mark.asyncio
async def test_send_to_source_room_is_opt_in_and_uses_source_thread_when_wake_enabled(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"send_to_source_room": True, "wake_bridge_enabled": True})

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!room:localhost"
    assert call.args[2] == "$thread"
    assert call.kwargs["trigger_dispatch"] is True


@pytest.mark.asyncio
async def test_dedupe_suppresses_duplicate_wake_notifications(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_room_id": "!parent:localhost"},
    )

    await hooks.notify_after_response(ctx)
    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert len(state["entries"]) == 1


def test_ledger_summary_never_persists_response_text(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    payload = hooks._completed_payload(_after_context(tmp_path, settings={"include_response_text": True}))

    summary = hooks._ledger_summary(payload)

    assert payload["response_text"] == "secret response text"
    assert "response_text" not in summary