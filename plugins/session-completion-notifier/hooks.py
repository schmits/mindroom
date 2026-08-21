"""Plugin-only terminal response notifications for MindRoom sessions.

This plugin intentionally uses only public hook contexts. It does not depend on
MindRoom response-lifecycle internals and therefore reports only the terminal
facts exposed by ``message:after_response`` and ``message:cancelled``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    AfterResponseContext,
    CancelledResponseContext,
    MessageEnvelope,
    hook,
)

_DEDUP_FILE = "dedupe.json"
_PARENT_LEDGER_FILE = "parent_ledger.json"
_DEDUP_STATE_VERSION = 1
_DEFAULT_DEDUP_MAX_ENTRIES = 512
_DEDUP_LOCKS: dict[str, asyncio.Lock] = {}
_DEDUP_LOCKS_GUARD = asyncio.Lock()
_LEDGER_LOCKS: dict[str, asyncio.Lock] = {}
_LEDGER_LOCKS_GUARD = asyncio.Lock()
_PARENT_LEDGER_STATE_EVENT_TYPE = "mindroom.session_completion.ledger"
_PARENT_LEDGER_STATE_VERSION = 1
_DEFAULT_PARENT_LEDGER_MAX_ENTRIES = 256


def _as_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_positive_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default


def _string_set(value: object) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = [str(item).strip() for item in value]
    else:
        return None
    normalized = {item for item in items if item}
    return normalized or None


def _enabled(settings: Mapping[str, object]) -> bool:
    return _as_bool(settings.get("enabled"), default=True)


def _in_configured_scope(settings: Mapping[str, object], envelope: MessageEnvelope) -> bool:
    allowed_agents = _string_set(settings.get("agents") or settings.get("allowed_agents"))
    if allowed_agents is not None and envelope.agent_name not in allowed_agents:
        return False

    allowed_rooms = _string_set(settings.get("rooms") or settings.get("allowed_rooms"))
    return not (allowed_rooms is not None and envelope.room_id not in allowed_rooms)


def _room_payload(envelope: MessageEnvelope) -> dict[str, str | None]:
    return {
        "id": envelope.room_id,
        "thread_id": envelope.target.resolved_thread_id,
        "source_thread_id": envelope.target.source_thread_id,
        "reply_to_event_id": envelope.target.reply_to_event_id,
    }


def _completed_payload(ctx: AfterResponseContext) -> dict[str, object]:
    result = ctx.result
    payload: dict[str, object] = {
        "status": "completed",
        "agent": result.envelope.agent_name,
        "room": _room_payload(result.envelope),
        "source_event_id": result.envelope.source_event_id,
        "response_event_id": result.response_event_id,
        "correlation_id": ctx.correlation_id,
        "response_kind": result.response_kind,
        "delivery": {
            "kind": result.delivery_kind,
            "failure_reason": None,
        },
    }
    if _as_bool(ctx.settings.get("include_response_text"), default=False):
        payload["response_text"] = result.response_text
    return payload


def _cancelled_payload(ctx: CancelledResponseContext) -> dict[str, object]:
    info = ctx.info
    status = "error" if info.failure_reason else "cancelled"
    return {
        "status": status,
        "agent": info.envelope.agent_name,
        "room": _room_payload(info.envelope),
        "source_event_id": info.envelope.source_event_id,
        "response_event_id": info.visible_response_event_id,
        "correlation_id": ctx.correlation_id,
        "response_kind": info.response_kind,
        "delivery": {
            "kind": "failed" if info.failure_reason else "cancelled",
            "failure_reason": info.failure_reason,
        },
    }


def _terminal_key(payload: Mapping[str, object]) -> str:
    delivery = payload.get("delivery")
    failure_reason = delivery.get("failure_reason") if isinstance(delivery, Mapping) else None
    parts = (
        payload.get("status"),
        payload.get("correlation_id"),
        payload.get("source_event_id"),
        payload.get("response_event_id"),
        failure_reason,
    )
    return "|".join("" if part is None else str(part) for part in parts)


def _as_non_empty_string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _parent_ledger_enabled(settings: Mapping[str, object]) -> bool:
    return _as_bool(settings.get("parent_ledger_enabled"), default=False)


def _parent_ledger_room_id(settings: Mapping[str, object], envelope: MessageEnvelope) -> str | None:
    room_id = _as_non_empty_string(settings.get("parent_ledger_room_id"))
    if room_id is not None:
        return room_id
    if _as_bool(settings.get("parent_ledger_to_source_room"), default=False):
        return envelope.room_id
    return None


def _parent_ledger_state_event_type(settings: Mapping[str, object]) -> str:
    return _as_non_empty_string(settings.get("parent_ledger_state_event_type")) or _PARENT_LEDGER_STATE_EVENT_TYPE


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _plugin_source_root() -> Path:
    return Path(__file__).resolve().parent


def _safe_state_root(ctx: AfterResponseContext | CancelledResponseContext) -> Path:
    """Return plugin state outside this plugin source tree, even for misbased runtime state."""
    source_root = _plugin_source_root()
    candidate = ctx.state_root.resolve()
    if not _path_is_relative_to(candidate, source_root):
        return candidate

    runtime_paths = ctx.runtime_paths
    if runtime_paths.control_state_root is not None:
        fallback_base = runtime_paths.control_state_root
    else:
        fallback_base = runtime_paths.storage_root / "control_state"
    fallback = (fallback_base / "plugins" / ctx.plugin_name).resolve()
    if _path_is_relative_to(fallback, source_root):
        fallback = (runtime_paths.config_dir / "mindroom_data" / "plugins" / ctx.plugin_name).resolve()
    if _path_is_relative_to(fallback, source_root):
        fallback = (source_root.parent / f".{ctx.plugin_name}-state").resolve()
    return fallback


def _parent_ledger_state_key(ctx: AfterResponseContext | CancelledResponseContext, envelope: MessageEnvelope) -> str:
    configured = _as_non_empty_string(ctx.settings.get("parent_ledger_state_key"))
    if configured is not None:
        return configured
    return envelope.agent_name or ctx.plugin_name


def _ledger_summary(payload: Mapping[str, object]) -> dict[str, object]:
    """Return minimized ledger material, deliberately excluding response text."""
    room = payload.get("room")
    room_id = room.get("id") if isinstance(room, Mapping) else None
    thread_id = room.get("thread_id") if isinstance(room, Mapping) else None
    delivery = payload.get("delivery")
    delivery_kind = delivery.get("kind") if isinstance(delivery, Mapping) else None
    failure_reason = delivery.get("failure_reason") if isinstance(delivery, Mapping) else None
    return {
        "key": _terminal_key(payload),
        "status": payload.get("status"),
        "agent": payload.get("agent"),
        "room_id": room_id,
        "thread_id": thread_id,
        "source_event_id": payload.get("source_event_id"),
        "response_event_id": payload.get("response_event_id"),
        "correlation_id": payload.get("correlation_id"),
        "response_kind": payload.get("response_kind"),
        "delivery_kind": delivery_kind,
        "failure_reason": failure_reason,
    }


def _coerce_parent_ledger(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, Mapping):
        return []
    raw_entries = raw.get("completions")
    if not isinstance(raw_entries, list):
        return []
    entries: list[dict[str, object]] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        entries.append(dict(entry))
    return entries


async def _ledger_lock(room_id: str, event_type: str, state_key: str) -> asyncio.Lock:
    lock_key = f"{room_id}\0{event_type}\0{state_key}"
    async with _LEDGER_LOCKS_GUARD:
        lock = _LEDGER_LOCKS.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _LEDGER_LOCKS[lock_key] = lock
        return lock


def _parent_ledger_content(
    entries: list[dict[str, object]],
    *,
    updated_at: float,
    max_entries: int,
) -> dict[str, object]:
    return {
        "version": _PARENT_LEDGER_STATE_VERSION,
        "updated_at": updated_at,
        "completions": entries[-max_entries:],
    }


def _merge_parent_ledger_entry(
    entries: list[dict[str, object]],
    summary: dict[str, object],
    *,
    now: float,
) -> list[dict[str, object]]:
    summary["updated_at"] = now
    for index, entry in enumerate(entries):
        if entry.get("key") == summary["key"]:
            first_seen_at = entry.get("first_seen_at")
            summary["first_seen_at"] = float(first_seen_at) if isinstance(first_seen_at, int | float) else now
            entries[index] = summary
            return entries
    summary["first_seen_at"] = now
    entries.append(summary)
    return entries


def _write_json_state(path: Path, content: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(content, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise


def _load_parent_ledger(path: Path) -> list[dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []
    return _coerce_parent_ledger(raw)


async def _update_plugin_parent_ledger(
    ctx: AfterResponseContext | CancelledResponseContext,
    payload: Mapping[str, object],
) -> None:
    max_entries = _as_positive_int(
        ctx.settings.get("parent_ledger_max_entries"),
        default=_DEFAULT_PARENT_LEDGER_MAX_ENTRIES,
    )
    state_path = _safe_state_root(ctx) / _PARENT_LEDGER_FILE
    try:
        async with await _dedupe_lock(state_path):
            now = time()
            entries = _merge_parent_ledger_entry(_load_parent_ledger(state_path), _ledger_summary(payload), now=now)
            _write_json_state(state_path, _parent_ledger_content(entries, updated_at=now, max_entries=max_entries))
    except Exception as exc:  # pragma: no cover - defensive isolation around plugin-local state.
        ctx.logger.warning("Session completion parent ledger state unavailable", error=str(exc))


async def _update_parent_ledger(
    ctx: AfterResponseContext | CancelledResponseContext,
    payload: Mapping[str, object],
    envelope: MessageEnvelope,
) -> None:
    """Best-effort Matrix-state parent ledger update using public hook helpers only."""
    if not _parent_ledger_enabled(ctx.settings):
        return
    room_id = _parent_ledger_room_id(ctx.settings, envelope)
    if room_id is None:
        await _update_plugin_parent_ledger(ctx, payload)
        return
    event_type = _parent_ledger_state_event_type(ctx.settings)
    state_key = _parent_ledger_state_key(ctx, envelope)
    max_entries = _as_positive_int(
        ctx.settings.get("parent_ledger_max_entries"),
        default=_DEFAULT_PARENT_LEDGER_MAX_ENTRIES,
    )
    try:
        async with await _ledger_lock(room_id, event_type, state_key):
            existing = await ctx.query_room_state(room_id, event_type, state_key)
            now = time()
            entries = _merge_parent_ledger_entry(_coerce_parent_ledger(existing), _ledger_summary(payload), now=now)
            content = _parent_ledger_content(entries, updated_at=now, max_entries=max_entries)
            ok = await ctx.put_room_state(room_id, event_type, state_key, content)
            if not ok:
                ctx.logger.warning("Session completion parent ledger write was not accepted")
    except Exception as exc:  # pragma: no cover - defensive isolation around optional bridge.
        ctx.logger.warning("Session completion parent ledger update failed", error=str(exc))


def _entry_key(entry: object) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        key = entry.get("key")
        return key if isinstance(key, str) else None
    return None


def _load_dedupe(path: Path) -> list[dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []

    raw_entries: object
    if isinstance(raw, list):
        # Backward compatibility with the original plugin-local list[str] state.
        raw_entries = raw
    elif isinstance(raw, Mapping):
        raw_entries = raw.get("entries")
    else:
        return []

    if not isinstance(raw_entries, list):
        return []

    entries: list[dict[str, object]] = []
    for raw_entry in raw_entries:
        key = _entry_key(raw_entry)
        if key is None:
            continue
        entry: dict[str, object] = {"key": key}
        if isinstance(raw_entry, Mapping):
            first_seen_at = raw_entry.get("first_seen_at")
            if isinstance(first_seen_at, int | float):
                entry["first_seen_at"] = float(first_seen_at)
        entries.append(entry)
    return entries


def _write_dedupe(path: Path, entries: list[dict[str, object]]) -> None:
    _write_json_state(path, {"version": _DEDUP_STATE_VERSION, "entries": entries})


async def _dedupe_lock(path: Path) -> asyncio.Lock:
    lock_key = str(path.resolve())
    async with _DEDUP_LOCKS_GUARD:
        lock = _DEDUP_LOCKS.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _DEDUP_LOCKS[lock_key] = lock
        return lock


async def _mark_seen(ctx: AfterResponseContext | CancelledResponseContext, payload: Mapping[str, object]) -> bool:
    if not _as_bool(ctx.settings.get("dedup_enabled"), default=True):
        return False
    key = _terminal_key(payload)
    try:
        dedupe_path = _safe_state_root(ctx) / _DEDUP_FILE
        async with await _dedupe_lock(dedupe_path):
            entries = _load_dedupe(dedupe_path)
            if any(entry.get("key") == key for entry in entries):
                return True
            max_entries = _as_positive_int(ctx.settings.get("dedup_max_entries"), default=_DEFAULT_DEDUP_MAX_ENTRIES)
            entries.append({"key": key, "first_seen_at": time()})
            _write_dedupe(dedupe_path, entries[-max_entries:])
    except Exception as exc:  # pragma: no cover - defensive isolation around plugin-local state.
        ctx.logger.warning("Session completion dedupe state unavailable", error=str(exc))
        return False
    return False


async def _emit_notification(
    ctx: AfterResponseContext | CancelledResponseContext,
    payload: dict[str, object],
    envelope: MessageEnvelope,
) -> None:
    if await _mark_seen(ctx, payload):
        ctx.logger.debug("Skipping duplicate session completion notification", payload=payload)
        return

    await _update_parent_ledger(ctx, payload, envelope)

    if _as_bool(ctx.settings.get("log_payload"), default=False):
        ctx.logger.info("Session completion notification", payload=payload)

    room_id = ctx.settings.get("notify_room_id")
    send_to_source = _as_bool(ctx.settings.get("send_to_source_room"), default=False)
    if not isinstance(room_id, str) or not room_id.strip():
        room_id = envelope.room_id if send_to_source else None
    if room_id is None:
        return

    configured_thread = ctx.settings.get("notify_thread_id")
    thread_id = configured_thread.strip() if isinstance(configured_thread, str) and configured_thread.strip() else None
    if thread_id is None and send_to_source:
        thread_id = envelope.target.resolved_thread_id

    try:
        await ctx.send_message(
            room_id.strip(),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            thread_id=thread_id,
            extra_content={"mindroom.session_completion": payload},
        )
    except Exception as exc:  # pragma: no cover - defensive isolation around transport adapters.
        ctx.logger.warning("Session completion notification send failed", error=str(exc), payload=payload)


@hook(EVENT_MESSAGE_AFTER_RESPONSE, name="notify_after_response", timeout_ms=1000)
async def notify_after_response(ctx: AfterResponseContext) -> None:
    """Emit a minimized terminal notification for completed visible responses."""
    if not _enabled(ctx.settings) or not _in_configured_scope(ctx.settings, ctx.result.envelope):
        return
    await _emit_notification(ctx, _completed_payload(ctx), ctx.result.envelope)


@hook(EVENT_MESSAGE_CANCELLED, name="notify_cancelled_response", timeout_ms=1000)
async def notify_cancelled_response(ctx: CancelledResponseContext) -> None:
    """Emit a minimized terminal notification for cancelled/error terminal outcomes."""
    if not _enabled(ctx.settings) or not _in_configured_scope(ctx.settings, ctx.info.envelope):
        return
    await _emit_notification(ctx, _cancelled_payload(ctx), ctx.info.envelope)


# Export pure helpers for plugin-local tests without depending on private runtime internals.
__all__ = [
    "_cancelled_payload",
    "_completed_payload",
    "_ledger_summary",
    "_safe_state_root",
    "notify_after_response",
    "notify_cancelled_response",
]