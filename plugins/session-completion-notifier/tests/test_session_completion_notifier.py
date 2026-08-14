"""Tests for the session completion notifier plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.hooks import (
    AfterResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    MessageEnvelope,
    ResponseResult,
)
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
        info=CancelledResponseInfo(
            envelope=_envelope(),
            visible_response_event_id="$partial",
            response_kind="ai",
            failure_reason=failure_reason,
        ),
    )


def test_completed_payload_minimizes_response_content(tmp_path: Path) -> None:
    """Completed payloads contain terminal metadata but not response text by default."""
    hooks = _load_hooks_module()
    payload = hooks._completed_payload(_after_context(tmp_path))

    assert payload["status"] == "completed"
    assert payload["agent"] == "mind"
    assert payload["room"]["id"] == "!room:localhost"
    assert payload["room"]["thread_id"] == "$thread"
    assert payload["source_event_id"] == "$source"
    assert payload["response_event_id"] == "$response"
    assert payload["correlation_id"] == "corr-1"
    assert payload["response_kind"] == "ai"
    assert payload["delivery"] == {"kind": "sent", "failure_reason": None}
    assert "response_text" not in payload


def test_completed_payload_can_opt_in_to_response_text(tmp_path: Path) -> None:
    """Response text is included only when explicitly enabled."""
    hooks = _load_hooks_module()
    payload = hooks._completed_payload(_after_context(tmp_path, settings={"include_response_text": True}))

    assert payload["response_text"] == "secret response text"


def test_cancelled_payload_distinguishes_error_terminal_outcome(tmp_path: Path) -> None:
    """Cancelled payloads report error status when a failure reason is available."""
    hooks = _load_hooks_module()
    payload = hooks._cancelled_payload(_cancelled_context(tmp_path, failure_reason="delivery failed"))

    assert payload["status"] == "error"
    assert payload["response_event_id"] == "$partial"
    assert payload["delivery"] == {"kind": "failed", "failure_reason": "delivery failed"}


@pytest.mark.asyncio
async def test_after_response_hook_can_send_matrix_notification_and_dedupes(tmp_path: Path) -> None:
    """The hook can send a JSON notification and suppress duplicate terminal events."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "log_payload": False},
    )

    await hooks.notify_after_response(ctx)
    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.args[0] == "!ops:localhost"
    assert '"response_text"' not in call.args[1]
    assert call.args[4]["mindroom.session_completion"]["status"] == "completed"
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert len(state["entries"]) == 1
    assert state["entries"][0]["key"].startswith("completed|corr-1|$source|$response|")


@pytest.mark.asyncio
async def test_dedupe_state_migrates_legacy_list_and_bounds_entries(tmp_path: Path) -> None:
    """Legacy list state remains readable and is rewritten as bounded structured state."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "log_payload": False, "dedup_max_entries": 2},
    )
    dedupe_path = ctx.state_root / "dedupe.json"
    dedupe_path.parent.mkdir(parents=True, exist_ok=True)
    dedupe_path.write_text('["legacy-key"]\n', encoding="utf-8")

    with patch.object(hooks, "time", return_value=1234.5):
        await hooks.notify_after_response(ctx)

    state = json.loads(dedupe_path.read_text(encoding="utf-8"))
    assert state == {
        "version": 1,
        "entries": [
            {"key": "legacy-key"},
            {"key": "completed|corr-1|$source|$response|", "first_seen_at": 1234.5},
        ],
    }


@pytest.mark.asyncio
async def test_dedupe_state_serializes_concurrent_terminal_events(tmp_path: Path) -> None:
    """Concurrent duplicate hook invocations share plugin-local state before sending."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "log_payload": False},
    )

    await asyncio.gather(hooks.notify_after_response(ctx), hooks.notify_after_response(ctx))

    assert ctx.message_sender.await_count == 1
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert len(state["entries"]) == 1


@pytest.mark.asyncio
async def test_dedupe_disabled_does_not_write_state(tmp_path: Path) -> None:
    """Operators can opt out of persistence when idempotency is handled elsewhere."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "log_payload": False, "dedup_enabled": False},
    )

    await hooks.notify_after_response(ctx)
    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 2
    assert not (ctx.state_root / "dedupe.json").exists()