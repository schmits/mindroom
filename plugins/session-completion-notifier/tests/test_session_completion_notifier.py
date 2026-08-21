"""Tests for the session completion notifier plugin."""

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


def test_ledger_summary_excludes_response_text(tmp_path: Path) -> None:
    """The parent-ledger representation stores only minimized terminal metadata."""
    hooks = _load_hooks_module()
    payload = hooks._completed_payload(_after_context(tmp_path, settings={"include_response_text": True}))

    summary = hooks._ledger_summary(payload)

    assert summary == {
        "key": "completed|corr-1|$source|$response|",
        "status": "completed",
        "agent": "mind",
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "source_event_id": "$source",
        "response_event_id": "$response",
        "correlation_id": "corr-1",
        "response_kind": "ai",
        "delivery_kind": "sent",
        "failure_reason": None,
    }
    assert "response_text" not in summary


@pytest.mark.asyncio
async def test_after_response_omitted_log_payload_does_not_log_payload_by_default(tmp_path: Path) -> None:
    """Omitting log_payload sends configured notifications without logging payload content."""
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"notify_room_id": "!ops:localhost"})
    logger = MagicMock()
    ctx.logger = logger

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_after_response_hook_can_send_matrix_notification_and_dedupes(tmp_path: Path) -> None:
    """The hook can send a minimized wake notification and suppress duplicate terminal events."""
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
    assert call.kwargs["thread_id"] is None
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    assert "secret response text" not in call.args[1]
    assert '"response_text"' not in call.args[1]
    assert '"correlation_id":"corr-1"' in call.args[1]
    assert '"source_event_id":"$source"' in call.args[1]
    assert '"response_event_id":"$response"' in call.args[1]
    assert call.kwargs["extra_content"]["mindroom.session_completion"]["status"] == "completed"
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
async def test_parent_ledger_updates_matrix_state_with_bounded_entries(tmp_path: Path) -> None:
    """The optional parent ledger is represented as bounded Matrix state content."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "parent_ledger_state_key": "parent-ledger",
            "parent_ledger_max_entries": 2,
            "notify_room_id": "!ops:localhost",
            "log_payload": False,
        },
    )
    ctx.room_state_querier.return_value = {
        "version": 1,
        "completions": [
            {"key": "older", "status": "completed"},
            {"key": "newer", "status": "completed"},
        ],
    }

    with patch.object(hooks, "time", return_value=4567.0):
        await hooks.notify_after_response(ctx)

    ctx.room_state_querier.assert_awaited_once_with(
        "!parent:localhost",
        "mindroom.session_completion.ledger",
        "parent-ledger",
    )
    ctx.room_state_putter.assert_awaited_once()
    call = ctx.room_state_putter.await_args
    assert call.args[:3] == (
        "!parent:localhost",
        "mindroom.session_completion.ledger",
        "parent-ledger",
    )
    content = call.args[3]
    assert content["version"] == 1
    assert content["updated_at"] == 4567.0
    assert [entry["key"] for entry in content["completions"]] == [
        "newer",
        "completed|corr-1|$source|$response|",
    ]
    terminal_entry = content["completions"][-1]
    assert terminal_entry["first_seen_at"] == 4567.0
    assert terminal_entry["updated_at"] == 4567.0
    assert "response_text" not in terminal_entry


@pytest.mark.asyncio
async def test_parent_ledger_replaces_duplicate_entry_without_growing(tmp_path: Path) -> None:
    """A repeated terminal key updates the existing parent-ledger entry in place."""
    hooks = _load_hooks_module()
    terminal_key = "completed|corr-1|$source|$response|"
    ctx = _after_context(
        tmp_path,
        settings={
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "dedup_enabled": False,
        },
    )
    ctx.room_state_querier.return_value = {
        "version": 1,
        "completions": [{"key": terminal_key, "first_seen_at": 111.0, "status": "completed"}],
    }

    with patch.object(hooks, "time", return_value=222.0):
        await hooks.notify_after_response(ctx)

    content = ctx.room_state_putter.await_args.args[3]
    assert len(content["completions"]) == 1
    assert content["completions"][0]["key"] == terminal_key
    assert content["completions"][0]["first_seen_at"] == 111.0
    assert content["completions"][0]["updated_at"] == 222.0


@pytest.mark.asyncio
async def test_parent_ledger_failure_is_isolated_from_notification(tmp_path: Path) -> None:
    """Ledger write failures are warning-only and do not block notification delivery."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "notify_room_id": "!ops:localhost",
        },
    )
    ctx.room_state_putter.side_effect = RuntimeError("state write failed")
    logger = MagicMock()
    ctx.logger = logger

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    logger.warning.assert_called()


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


@pytest.mark.asyncio
async def test_parent_ledger_enabled_without_room_writes_plugin_state(tmp_path: Path) -> None:
    """Without a Matrix ledger room, parent ledger falls back to plugin-local state."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_max_entries": 2, "dedup_enabled": False},
    )

    with patch.object(hooks, "time", return_value=3456.0):
        await hooks.notify_after_response(ctx)

    ctx.room_state_querier.assert_not_awaited()
    ctx.room_state_putter.assert_not_awaited()
    state_path = ctx.state_root / "parent_ledger.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["updated_at"] == 3456.0
    assert [entry["key"] for entry in state["completions"]] == ["completed|corr-1|$source|$response|"]
    assert "response_text" not in state["completions"][0]


@pytest.mark.asyncio
async def test_parent_ledger_plugin_state_is_bounded_idempotent_and_tolerates_malformed_state(tmp_path: Path) -> None:
    """Plugin-local parent ledger ignores malformed state and rewrites bounded content."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_max_entries": 1, "dedup_enabled": False},
    )
    state_path = ctx.state_root / "parent_ledger.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "completions": [
                    {
                        "key": "old",
                        "status": "completed",
                        "agent": "legacy-agent",
                        "first_seen_at": 50.0,
                        "response_text": "secret",
                        "extra": "leak",
                    },
                    42,
                    {"key": ""},
                ],
                "extra": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.object(hooks, "time", side_effect=[100.0, 200.0]):
        await hooks.notify_after_response(ctx)
        await hooks.notify_after_response(ctx)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["completions"]) == 1
    entry = state["completions"][0]
    assert entry["key"] == "completed|corr-1|$source|$response|"
    assert entry["status"] == "completed"
    assert entry["agent"] == "mind"
    assert entry["first_seen_at"] == 100.0
    assert entry["updated_at"] == 200.0
    assert "response_text" not in json.dumps(state, sort_keys=True)
    assert "extra" not in json.dumps(state, sort_keys=True)


@pytest.mark.asyncio
async def test_parent_ledger_matrix_state_drops_unknown_existing_entry_fields(tmp_path: Path) -> None:
    """Matrix parent ledger state is sanitized before being mirrored back."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "parent_ledger_max_entries": 2,
            "dedup_enabled": False,
        },
    )
    ctx.room_state_querier = AsyncMock(
        return_value={
            "completions": [
                {
                    "key": "old",
                    "status": "completed",
                    "agent": "legacy-agent",
                    "first_seen_at": 50.0,
                    "response_text": "secret",
                    "extra": "leak",
                }
            ]
        }
    )

    with patch.object(hooks, "time", return_value=123.0):
        await hooks.notify_after_response(ctx)

    content = ctx.room_state_putter.await_args.args[3]
    assert [entry["key"] for entry in content["completions"]] == ["old", "completed|corr-1|$source|$response|"]
    legacy_entry = content["completions"][0]
    assert legacy_entry == {
        "key": "old",
        "status": "completed",
        "agent": "legacy-agent",
        "first_seen_at": 50.0,
    }
    terminal_entry = content["completions"][1]
    assert terminal_entry["status"] == "completed"
    assert terminal_entry["agent"] == "mind"
    assert terminal_entry["first_seen_at"] == 123.0
    assert terminal_entry["updated_at"] == 123.0
    assert "response_text" not in json.dumps(content, sort_keys=True)
    assert "extra" not in json.dumps(content, sort_keys=True)


@pytest.mark.asyncio
async def test_matrix_mirror_is_explicit_opt_in_and_minimized(tmp_path: Path) -> None:
    """The parent-ledger Matrix mirror only runs when a destination room is explicitly configured."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={
            "include_response_text": True,
            "parent_ledger_enabled": True,
            "parent_ledger_room_id": "!parent:localhost",
            "dedup_enabled": False,
        },
    )

    await hooks.notify_after_response(ctx)

    assert not (ctx.state_root / "parent_ledger.json").exists()
    content = ctx.room_state_putter.await_args.args[3]
    assert len(content["completions"]) == 1
    assert "response_text" not in content["completions"][0]
    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.args[0] == "!parent:localhost"
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    assert "secret response text" not in call.args[1]
    assert "response_text" not in call.args[1]


@pytest.mark.asyncio
async def test_safe_state_root_avoids_plugin_source_when_runtime_state_points_at_source(tmp_path: Path) -> None:
    """Dedupe/ledger state never writes inside the watched plugin source tree."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "notify_room_id": "!ops:localhost"},
    )
    ctx.runtime_paths = runtime_paths_with_storage_root(ctx.runtime_paths, PLUGIN_ROOT.parent.parent)

    assert ctx.state_root.resolve() == PLUGIN_ROOT.resolve()
    safe_root = hooks._safe_state_root(ctx)
    assert PLUGIN_ROOT.resolve() not in [safe_root, *safe_root.parents]

    await hooks.notify_after_response(ctx)

    assert not (PLUGIN_ROOT / "dedupe.json").exists()
    assert not (PLUGIN_ROOT / "parent_ledger.json").exists()
    assert (safe_root / "dedupe.json").is_file()
    assert (safe_root / "parent_ledger.json").is_file()


@pytest.mark.asyncio
async def test_no_response_text_by_default_even_when_response_has_text(tmp_path: Path) -> None:
    """Notifications and plugin-local ledger omit response text unless explicit payload opt-in is set."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "parent_ledger_enabled": True, "dedup_enabled": False},
    )

    await hooks.notify_after_response(ctx)

    notify_body = ctx.message_sender.await_args.args[1]
    ledger = json.loads((ctx.state_root / "parent_ledger.json").read_text(encoding="utf-8"))
    assert "secret response text" not in notify_body
    assert "response_text" not in notify_body
    assert "response_text" not in ledger["completions"][0]


@pytest.mark.asyncio
async def test_reload_churn_reuses_plugin_state_without_duplicate_notify(tmp_path: Path) -> None:
    """Reloaded hook modules still honor existing plugin-local dedupe state."""
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"notify_room_id": "!ops:localhost"})
    await hooks.notify_after_response(ctx)

    reloaded_hooks = _load_hooks_module()
    await reloaded_hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    state = json.loads((ctx.state_root / "dedupe.json").read_text(encoding="utf-8"))
    assert len(state["entries"]) == 1


@pytest.mark.asyncio
async def test_concurrent_plugin_parent_ledger_updates_are_idempotent(tmp_path: Path) -> None:
    """Concurrent duplicate parent-ledger updates share plugin-local state before writing."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "dedup_enabled": False},
    )

    await asyncio.gather(hooks.notify_after_response(ctx), hooks.notify_after_response(ctx))

    state = json.loads((ctx.state_root / "parent_ledger.json").read_text(encoding="utf-8"))
    assert len(state["completions"]) == 1
    assert state["completions"][0]["key"] == "completed|corr-1|$source|$response|"


@pytest.mark.asyncio
async def test_parent_ledger_room_is_wake_destination_when_notify_room_omitted(tmp_path: Path) -> None:
    """A configured parent-ledger room doubles as the minimized wake destination by default."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True, "parent_ledger_room_id": "!parent:localhost"},
    )

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 1
    call = ctx.message_sender.await_args
    assert call.args[0] == "!parent:localhost"
    assert call.kwargs["thread_id"] is None
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")
    assert '"correlation_id":"corr-1"' in call.args[1]
    assert '"room_id":"!room:localhost"' in call.args[1]
    assert "secret response text" not in call.args[1]
    assert "response_text" not in call.args[1]


@pytest.mark.asyncio
async def test_no_matrix_notification_when_no_destination_configured(tmp_path: Path) -> None:
    """Plain plugin-local parent ledger writes stay passive when no notify or parent room is configured."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"parent_ledger_enabled": True},
    )

    await hooks.notify_after_response(ctx)

    assert ctx.message_sender.await_count == 0
    assert (ctx.state_root / "parent_ledger.json").is_file()


@pytest.mark.asyncio
async def test_wake_bridge_can_be_disabled_for_legacy_json_notification(tmp_path: Path) -> None:
    """Operators can keep the prior passive JSON notification body if explicitly configured."""
    hooks = _load_hooks_module()
    ctx = _after_context(
        tmp_path,
        settings={"notify_room_id": "!ops:localhost", "wake_bridge_enabled": False},
    )

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.kwargs["trigger_dispatch"] is False
    assert call.args[1].startswith("{")
    assert "@mindroom_mind_mm3j9z5u:mindroom.chat" not in call.args[1]


@pytest.mark.asyncio
async def test_send_to_source_room_wake_bridge_uses_source_thread(tmp_path: Path) -> None:
    """Source-room delivery remains opt-in and includes dispatchable minimized wake text."""
    hooks = _load_hooks_module()
    ctx = _after_context(tmp_path, settings={"send_to_source_room": True})

    await hooks.notify_after_response(ctx)

    call = ctx.message_sender.await_args
    assert call.args[0] == "!room:localhost"
    assert call.kwargs["thread_id"] == "$thread"
    assert call.kwargs["trigger_dispatch"] is True
    assert call.args[1].startswith("@mindroom_mind_mm3j9z5u:mindroom.chat session completion: ")