"""Regression tests for session-completion wake bridge intent gating."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_hooks_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_completion_notifier_hooks_wake_gate", PLUGIN_ROOT / "hooks.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wake_bridge_is_passive_without_explicit_or_parent_ledger_intent() -> None:
    """Notify-only settings must not broaden into wake dispatch by default."""
    hooks = _load_hooks_module()

    assert hooks._wake_bridge_enabled({"notify_room_id": "!ops:localhost"}) is False
    assert hooks._wake_bridge_enabled({"send_to_source_room": True}) is False


def test_wake_bridge_honors_explicit_override() -> None:
    """Operators can explicitly opt wake dispatch in or out."""
    hooks = _load_hooks_module()

    assert hooks._wake_bridge_enabled({"notify_room_id": "!ops:localhost", "wake_bridge_enabled": True}) is True
    assert hooks._wake_bridge_enabled({"parent_ledger_enabled": True, "notify_room_id": "!ops:localhost", "wake_bridge_enabled": False}) is False


def test_parent_ledger_intent_still_wakes_when_destination_is_configured() -> None:
    """Existing parent-ledger plus Matrix-destination intent remains wake-enabled."""
    hooks = _load_hooks_module()

    assert hooks._wake_bridge_enabled({"parent_ledger_enabled": True, "notify_room_id": "!ops:localhost"}) is True
    assert hooks._wake_bridge_enabled({"parent_ledger_enabled": True, "parent_ledger_room_id": "!parent:localhost"}) is True
    assert hooks._wake_bridge_enabled({"parent_ledger_enabled": True, "parent_ledger_to_source_room": True}) is True
    assert hooks._wake_bridge_enabled({"parent_ledger_enabled": True}) is False