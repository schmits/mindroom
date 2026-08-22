"""Focused tests for the session completion notifier plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, runtime_paths_with_storage_root
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.hooks import AfterResponseContext, CancelledResponseContext, CancelledResponseInfo, MessageEnvelope, ResponseResult
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import classify_turn_origin

if TYPE_CHECKING:
    from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MIND_MENTION = "@mindroom_mind_mm3j9z5u:mindroom.chat"


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


def _envelope(*, agent_name: str = "mind", room_id: str = "!room:localhost") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$source",
        target=MessageTarget.resolve(room_id, "$thread", "$source"),
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
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


def _cancelled_context(tmp_path: Path, *, failure_reason: str | None = None) -> CancelledResponseContext:
    config, runtime_paths = _config(tmp_path)
    return CancelledResponseContext(
        event_name="message:cancelled",
        plugin_name="session-completion-notifier",
        settings={},
        config=config,
        runtime_paths=runtime_paths,
        logger=get_logger("tests.session_completion_notifier"),
        correlation_id="corr-2",
        message_sender=AsyncMock(return_value="$notice"),
        room_state_querier=AsyncMock(return_value=None),
        room_state_putter=AsyncMock(return_value=True),
        info=CancelledResponseInfo(
            envelope=_envelope(),
            visible_response_event_id="$partial",
            response_kind="ai",
            failure_reason=failure_reason,
        ),
    )


def test_payloads_and_ledger_summary_are_minimized_by_default(tmp_path: Path) -> None:
    hooks = _load_hooks_module()

    completed = hooks._completed_payload(_after_context(tmp_path))
    cancelled = hooks._cancelled_payload(_cancelled_context(tmp_path, failure_reason="delivery failed"))
    opted_in = hooks._completed_payload(_after_context(tmp_path, settings={"include_response_text": True}))
    summary = hooks._ledger_summary(opted_in)

    assert completed["status"] == "completed"
    assert completed["room"]["id"] == "!room:localhost"
    assert completed["room"]["thread_id"] == "$thread"
    assert completed["source_event_id"] == "$source"
    assert completed["response_event_id"] == "$response"
    assert completed["correlation_id"] == "corr-1"
    assert completed["delivery"] == {"kind": "sent", "failure_reason": None}
    assert "response_text" not in completed
    assert cancelled["status"] == "error"
    assert cancelled["delivery"] == {"kind": "failed", "failure_reason": "delivery failed"}
    assert opted_in["response_text"] == "secret response text"
    assert summary["key"] == "completed|corr-1|$source|$response|"
    assert summary["room_id"] == "!room:localhost"
    assert summary["thread_id"] == "$thread"
    assert "response_text" not in summary


@pytest.mark.asyncio
async def test_explicit_wake_notification_has_mention_metadata_no_text_and_dedupes(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "log_payload": False, "wake_bridge_enabled": True},
    )
    logger = MagicMock()
    ctx.logger = logger

    await hooks.notify_after_response(ctx)
    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.args[0] == "!ops:localhost"
    assert call.args[2] is None
    assert call.kwargs["trigger_dispatch"] is True
    body = call.args[1]
    assert body.startswith(f"{MIND_MENTION} session completion: ")
    assert '"correlation_id":"corr-1"' in body
    assert '"source_event_id":"$source"' in body
    assert '"response_event_id":"$response"' in body
    assert '"room_id":"!room:localhost"' in body
    assert "secret response text" not in body
    assert '"response_text"' not in body
    assert call.args[4]["mindroom.session_completion"]["status"] == "completed"
    logger.info.assert_not_called()
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert len(state["entries"]) == 1


@pytest.mark.asyncio
async def test_intent_gated_parent_ledger_room_wakes_when_flag_omitted(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_room_id": "!parent:localhost"},
    )

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.args[0] == "!parent:localhost"
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith(f"{MIND_MENTION} session completion: ")
    assert "secret response text" not in call.args[1]
    content = ctx.room_state_putter.await_args.args[3]
    assert content["completions"][0]["key"] == "completed|corr-1|$source|$response|"
    assert "response_text" not in json.dumps(content, sort_keys=True)


@pytest.mark.asyncio
async def test_no_destination_keeps_parent_ledger_passive_without_wake(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"parent_ledger_enabled": True})

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert ctx.room_state_putter.await_count == 0
    ledger = json.loads((ctx.state_root / "parent_ledger.json").read_text(encoding="utf-8"))
    assert ledger["completions"][0]["key"] == "completed|corr-1|$source|$response|"
    assert "response_text" not in json.dumps(ledger, sort_keys=True)


@pytest.mark.asyncio
async def test_explicit_wake_bridge_false_keeps_legacy_passive_notification(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "notify_room_id": "!ops:localhost",
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "wake_bridge_enabled": False,
        },
    )

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!ops:localhost"
    assert call.kwargs["trigger_dispatch"] is False
    assert call.args[1].startswith("{")
    assert MIND_MENTION not in call.args[1]
    assert ctx.room_state_putter.await_count == 1


@pytest.mark.asyncio
async def test_send_to_source_room_is_opt_in_and_uses_source_thread(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"send_to_source_room": True, "wake_bridge_enabled": True})

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!room:localhost"
    assert call.args[2] == "$thread"
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith(f"{MIND_MENTION} session completion: ")


@pytest.mark.asyncio
async def test_source_room_default_is_not_used_without_explicit_opt_in(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"wake_bridge_enabled": True})

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert not (ctx.state_root / "parent_ledger.json").exists()


@pytest.mark.asyncio
async def test_safe_state_root_avoids_plugin_source_and_reload_dedupe_prevents_spam(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "parent_ledger_enabled": True,
            "notify_room_id": "!ops:localhost",
            "wake_bridge_enabled": True,
            "dedup_enabled": True,
        },
    )
    ctx.runtime_paths = runtime_paths_with_storage_root(ctx.runtime_paths, PLUGIN_ROOT.parent.parent)

    assert ctx.state_root.resolve() == PLUGIN_ROOT.resolve()
    safe_root = hooks._safe_state_root(ctx)
    assert PLUGIN_ROOT.resolve() not in [safe_root, *safe_root.parents]

    await hooks.notify_after_response(ctx)
    await _load_hooks_module().notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    assert not (PLUGIN_ROOT / "dedupe.json").exists()
    assert not (PLUGIN_ROOT / "parent_ledger.json").exists()
    assert (safe_root / "dedupe.json").is_file()
    assert (safe_root / "parent_ledger.json").is_file()


@pytest.mark.asyncio
async def test_concurrent_duplicate_terminal_events_do_not_spam(tmp_path: Path) -> None:
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"notify_room_id": "!ops:localhost", "wake_bridge_enabled": True})

    await asyncio.gather(hooks.notify_after_response(ctx), hooks.notify_after_response(ctx))

    assert ctx.message_sender.await_count == 1
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert len(state["entries"]) == 1