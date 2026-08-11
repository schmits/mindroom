"""Tests for metadata-only workspace lease registry."""
# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.workspace_leases import (
    WORKSPACE_LEASE_REGISTRY_RELATIVE_PATH,
    WorkspaceLeaseRegistry,
    WorkspaceLeaseRegistryError,
    workspace_lease_registry_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_acquire_persists_metadata_without_creating_workspace_root(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)
    workspace_root = tmp_path / "agents" / "code" / "workspace"

    lease = registry.acquire(
        workspace_root,
        owner_kind="agent",
        owner_id="code",
        purpose="tool output workspace",
        metadata={"worker_key": "agent:code", "attempt": 1, "trusted": True, "empty": None},
        ttl_seconds=60,
        now=100.0,
        lease_id="lease-1",
    )

    assert lease.lease_id == "lease-1"
    assert lease.status == "active"
    assert lease.expires_at == 160.0
    assert not workspace_root.exists()

    registry_path = tmp_path / WORKSPACE_LEASE_REGISTRY_RELATIVE_PATH
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "leases": {
            "lease-1": {
                "lease_id": "lease-1",
                "workspace_root": str(workspace_root.resolve()),
                "owner_kind": "agent",
                "owner_id": "code",
                "purpose": "tool output workspace",
                "status": "active",
                "metadata": {"attempt": 1, "empty": None, "trusted": True, "worker_key": "agent:code"},
                "created_at": 100.0,
                "updated_at": 100.0,
                "expires_at": 160.0,
                "released_at": None,
            },
        },
    }


def test_list_marks_elapsed_leases_expired_in_memory(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)
    registry.acquire(
        tmp_path / "agents" / "code" / "workspace",
        owner_kind="agent",
        owner_id="code",
        ttl_seconds=10,
        now=100.0,
        lease_id="lease-1",
    )

    leases = registry.list(now=111.0)

    assert [lease.status for lease in leases] == ["expired"]
    assert registry.list(include_inactive=False, now=111.0) == ()


def test_prune_expired_persists_lifecycle_transition(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)
    registry.acquire(
        tmp_path / "agents" / "code" / "workspace",
        owner_kind="agent",
        owner_id="code",
        ttl_seconds=10,
        now=100.0,
        lease_id="lease-1",
    )

    expired = registry.prune_expired(now=111.0)

    assert [lease.lease_id for lease in expired] == ["lease-1"]
    assert expired[0].status == "expired"
    payload = json.loads(workspace_lease_registry_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["leases"]["lease-1"]["status"] == "expired"
    assert payload["leases"]["lease-1"]["updated_at"] == 111.0


def test_release_is_idempotent_and_retains_metadata(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)
    registry.acquire(
        tmp_path / "agents" / "code" / "workspace",
        owner_kind="agent",
        owner_id="code",
        now=100.0,
        lease_id="lease-1",
    )

    released = registry.release("lease-1", now=125.0)
    released_again = registry.release("lease-1", now=130.0)

    assert released.status == "released"
    assert released.released_at == 125.0
    assert released_again == released
    assert registry.list(now=130.0)[0].status == "released"


def test_refresh_extends_active_lease(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)
    registry.acquire(
        tmp_path / "agents" / "code" / "workspace",
        owner_kind="agent",
        owner_id="code",
        ttl_seconds=10,
        now=100.0,
        lease_id="lease-1",
    )

    refreshed = registry.refresh("lease-1", ttl_seconds=30, now=105.0)

    assert refreshed.updated_at == 105.0
    assert refreshed.expires_at == 135.0


def test_rejects_workspace_outside_storage_root(tmp_path: Path) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path / "storage")

    with pytest.raises(WorkspaceLeaseRegistryError, match="workspace_root must stay within storage_root"):
        registry.acquire(
            tmp_path / "outside" / "workspace",
            owner_kind="agent",
            owner_id="code",
            lease_id="lease-1",
        )


@pytest.mark.parametrize(
    ("metadata", "match"),
    [
        ({"nested": {"nope": True}}, "metadata values must be scalar JSON values"),
        ({"x" * 129: "value"}, "metadata keys must be at most"),
        ({"value": "x" * 1025}, "metadata values must be at most"),
    ],
)
def test_validates_metadata(tmp_path: Path, metadata: dict[str, object], match: str) -> None:
    registry = WorkspaceLeaseRegistry(tmp_path)

    with pytest.raises(WorkspaceLeaseRegistryError, match=match):
        registry.acquire(
            tmp_path / "agents" / "code" / "workspace",
            owner_kind="agent",
            owner_id="code",
            metadata=metadata,
            lease_id="lease-1",
        )


def test_ignores_corrupt_registry_payload(tmp_path: Path) -> None:
    registry_path = workspace_lease_registry_path(tmp_path)
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("not json", encoding="utf-8")

    assert WorkspaceLeaseRegistry(tmp_path).list() == ()