"""Metadata-only workspace lease lifecycle registry.

The registry records best-effort ownership/lifecycle metadata for workspace roots.
It is intentionally not an authorization boundary and does not create, delete, or
otherwise mutate workspace contents.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

WORKSPACE_LEASE_REGISTRY_VERSION = 1
WORKSPACE_LEASE_REGISTRY_RELATIVE_PATH = Path(".runtime") / "workspace_leases.json"
_WORKSPACE_LEASE_REGISTRY_LOCK_SUFFIX = ".lock"
_MAX_OWNER_KIND_LENGTH = 64
_MAX_OWNER_ID_LENGTH = 256
_MAX_PURPOSE_LENGTH = 256
_MAX_METADATA_KEY_LENGTH = 128
_MAX_METADATA_VALUE_LENGTH = 1024
_MAX_METADATA_ITEMS = 32

WorkspaceLeaseStatus = Literal["active", "released", "expired"]
WorkspaceLeaseMetadataValue = str | int | float | bool | None


class WorkspaceLeaseRegistryError(ValueError):
    """Raised when workspace lease registry input is invalid."""


class WorkspaceLeaseRecordPayload(TypedDict, total=False):
    """JSON payload for one workspace lease record."""

    lease_id: str
    workspace_root: str
    owner_kind: str
    owner_id: str
    purpose: str | None
    status: WorkspaceLeaseStatus
    metadata: dict[str, WorkspaceLeaseMetadataValue]
    created_at: float
    updated_at: float
    expires_at: float | None
    released_at: float | None


class WorkspaceLeaseRegistryPayload(TypedDict):
    """JSON payload for the workspace lease registry."""

    version: int
    leases: dict[str, WorkspaceLeaseRecordPayload]


@dataclass(frozen=True)
class WorkspaceLeaseRecord:
    """One metadata-only workspace lifecycle lease."""

    lease_id: str
    workspace_root: Path
    owner_kind: str
    owner_id: str
    purpose: str | None
    status: WorkspaceLeaseStatus
    metadata: Mapping[str, WorkspaceLeaseMetadataValue]
    created_at: float
    updated_at: float
    expires_at: float | None = None
    released_at: float | None = None

    def is_active_at(self, now: float | None = None) -> bool:
        """Return whether this lease is active at ``now`` without mutating it."""
        check_time = _current_time() if now is None else now
        return self.status == "active" and (self.expires_at is None or self.expires_at > check_time)


@dataclass(frozen=True)
class WorkspaceLeaseRegistry:
    """Durable metadata-only registry rooted under one MindRoom storage path."""

    storage_root: Path
    registry_path: Path | None = None

    def __post_init__(self) -> None:
        """Normalize registry paths after dataclass initialization."""
        object.__setattr__(self, "storage_root", self.storage_root.expanduser().resolve())
        registry_path = self.registry_path
        if registry_path is None:
            registry_path = self.storage_root / WORKSPACE_LEASE_REGISTRY_RELATIVE_PATH
        object.__setattr__(self, "registry_path", registry_path.expanduser().resolve())

    @property
    def lock_path(self) -> Path:
        """Return the advisory lock path for registry mutations."""
        registry_path = _require_registry_path(self.registry_path)
        return registry_path.with_name(f"{registry_path.name}{_WORKSPACE_LEASE_REGISTRY_LOCK_SUFFIX}")

    def acquire(
        self,
        workspace_root: Path,
        *,
        owner_kind: str,
        owner_id: str,
        purpose: str | None = None,
        metadata: Mapping[str, object] | None = None,
        ttl_seconds: float | None = None,
        now: float | None = None,
        lease_id: str | None = None,
    ) -> WorkspaceLeaseRecord:
        """Record a new active workspace lease.

        The call only writes registry metadata under ``storage_root/.runtime``; it
        never creates or modifies ``workspace_root`` itself.
        """
        timestamp = _current_time() if now is None else now
        normalized_ttl = _validate_ttl_seconds(ttl_seconds)
        record = WorkspaceLeaseRecord(
            lease_id=_validate_lease_id(lease_id or uuid.uuid4().hex),
            workspace_root=_resolve_workspace_root(workspace_root, storage_root=self.storage_root),
            owner_kind=_validate_limited_text(owner_kind, "owner_kind", max_length=_MAX_OWNER_KIND_LENGTH),
            owner_id=_validate_limited_text(owner_id, "owner_id", max_length=_MAX_OWNER_ID_LENGTH),
            purpose=_validate_optional_limited_text(purpose, "purpose", max_length=_MAX_PURPOSE_LENGTH),
            status="active",
            metadata=_validate_metadata(metadata or {}),
            created_at=timestamp,
            updated_at=timestamp,
            expires_at=(timestamp + normalized_ttl) if normalized_ttl is not None else None,
            released_at=None,
        )
        with advisory_file_lock(self.lock_path):
            records = self._read_records_unlocked()
            if record.lease_id in records:
                msg = f"workspace lease already exists: {record.lease_id}"
                raise WorkspaceLeaseRegistryError(msg)
            records[record.lease_id] = record
            self._write_records_unlocked(records)
        return record

    def refresh(
        self,
        lease_id: str,
        *,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> WorkspaceLeaseRecord:
        """Refresh one active lease's heartbeat and optional expiry."""
        normalized_lease_id = _validate_lease_id(lease_id)
        timestamp = _current_time() if now is None else now
        normalized_ttl = _validate_ttl_seconds(ttl_seconds)
        with advisory_file_lock(self.lock_path):
            records = self._read_records_unlocked()
            record = _lease_with_effective_status(_require_record(records, normalized_lease_id), now=timestamp)
            if record.status != "active":
                records[normalized_lease_id] = record
                self._write_records_unlocked(records)
                msg = f"workspace lease is not active: {normalized_lease_id}"
                raise WorkspaceLeaseRegistryError(msg)
            refreshed = replace(
                record,
                updated_at=timestamp,
                expires_at=(timestamp + normalized_ttl) if normalized_ttl is not None else record.expires_at,
            )
            records[normalized_lease_id] = refreshed
            self._write_records_unlocked(records)
        return refreshed

    def release(self, lease_id: str, *, now: float | None = None) -> WorkspaceLeaseRecord:
        """Mark one lease as released while retaining lifecycle metadata."""
        normalized_lease_id = _validate_lease_id(lease_id)
        timestamp = _current_time() if now is None else now
        with advisory_file_lock(self.lock_path):
            records = self._read_records_unlocked()
            record = _require_record(records, normalized_lease_id)
            if record.status == "released":
                return record
            released = replace(record, status="released", updated_at=timestamp, released_at=timestamp)
            records[normalized_lease_id] = released
            self._write_records_unlocked(records)
        return released

    def list(self, *, include_inactive: bool = True, now: float | None = None) -> tuple[WorkspaceLeaseRecord, ...]:
        """Return leases sorted by creation time and id."""
        timestamp = _current_time() if now is None else now
        with advisory_file_lock(self.lock_path, exclusive=False):
            records = self._read_records_unlocked()
        values: Iterable[WorkspaceLeaseRecord] = (
            _lease_with_effective_status(record, now=timestamp) for record in records.values()
        )
        if not include_inactive:
            values = (record for record in values if record.status == "active")
        return tuple(sorted(values, key=lambda record: (record.created_at, record.lease_id)))

    def prune_expired(self, *, now: float | None = None) -> tuple[WorkspaceLeaseRecord, ...]:
        """Mark elapsed active leases as expired and persist the lifecycle transition."""
        timestamp = _current_time() if now is None else now
        with advisory_file_lock(self.lock_path):
            records = self._read_records_unlocked()
            expired: list[WorkspaceLeaseRecord] = []
            for lease_id, record in records.items():
                if record.status == "active" and record.expires_at is not None and record.expires_at <= timestamp:
                    expired_record = replace(record, status="expired", updated_at=timestamp)
                    records[lease_id] = expired_record
                    expired.append(expired_record)
            if expired:
                self._write_records_unlocked(records)
        return tuple(sorted(expired, key=lambda record: (record.created_at, record.lease_id)))

    def _read_records_unlocked(self) -> dict[str, WorkspaceLeaseRecord]:
        registry_path = _require_registry_path(self.registry_path)
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        leases = payload.get("leases")
        if not isinstance(leases, dict):
            return {}
        records: dict[str, WorkspaceLeaseRecord] = {}
        for lease_id, record_payload in leases.items():
            if not isinstance(lease_id, str) or not isinstance(record_payload, dict):
                continue
            try:
                record = _record_from_payload(lease_id, record_payload, storage_root=self.storage_root)
            except WorkspaceLeaseRegistryError:
                continue
            records[record.lease_id] = record
        return records

    def _write_records_unlocked(self, records: Mapping[str, WorkspaceLeaseRecord]) -> None:
        registry_path = _require_registry_path(self.registry_path)
        payload: WorkspaceLeaseRegistryPayload = {
            "version": WORKSPACE_LEASE_REGISTRY_VERSION,
            "leases": {lease_id: _record_to_payload(record) for lease_id, record in sorted(records.items())},
        }
        write_json_file_durable(registry_path, payload, indent=2, sort_keys=True, trailing_newline=True)


def workspace_lease_registry_path(storage_root: Path) -> Path:
    """Return the default durable registry path for ``storage_root``."""
    return storage_root.expanduser().resolve() / WORKSPACE_LEASE_REGISTRY_RELATIVE_PATH


def _current_time() -> float:
    return time.time()


def _require_registry_path(registry_path: Path | None) -> Path:
    if registry_path is None:  # pragma: no cover - dataclass invariant guard
        msg = "workspace lease registry path was not initialized"
        raise WorkspaceLeaseRegistryError(msg)
    return registry_path


def _resolve_workspace_root(workspace_root: Path, *, storage_root: Path) -> Path:
    resolved = workspace_root.expanduser().resolve()
    try:
        resolved.relative_to(storage_root)
    except ValueError:
        msg = f"workspace_root must stay within storage_root: {storage_root}"
        raise WorkspaceLeaseRegistryError(msg) from None
    return resolved


def _validate_lease_id(lease_id: str) -> str:
    value = _validate_limited_text(lease_id, "lease_id", max_length=128)
    if any(character in value for character in ("/", "\\", "\x00")):
        msg = "lease_id must not contain path separators or NUL bytes"
        raise WorkspaceLeaseRegistryError(msg)
    return value


def _validate_limited_text(value: object, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise WorkspaceLeaseRegistryError(msg)
    normalized = value.strip()
    if not normalized:
        msg = f"{field_name} must not be empty"
        raise WorkspaceLeaseRegistryError(msg)
    if len(normalized) > max_length:
        msg = f"{field_name} must be at most {max_length} characters"
        raise WorkspaceLeaseRegistryError(msg)
    return normalized


def _validate_optional_limited_text(value: object, field_name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _validate_limited_text(value, field_name, max_length=max_length)


def _validate_ttl_seconds(ttl_seconds: float | None) -> float | None:
    if ttl_seconds is None:
        return None
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int | float):
        msg = "ttl_seconds must be a number when provided"
        raise WorkspaceLeaseRegistryError(msg)
    ttl = float(ttl_seconds)
    if ttl <= 0:
        msg = "ttl_seconds must be greater than zero"
        raise WorkspaceLeaseRegistryError(msg)
    return ttl


def _validate_metadata(metadata: Mapping[str, object]) -> dict[str, WorkspaceLeaseMetadataValue]:
    if len(metadata) > _MAX_METADATA_ITEMS:
        msg = f"metadata must contain at most {_MAX_METADATA_ITEMS} items"
        raise WorkspaceLeaseRegistryError(msg)
    normalized: dict[str, WorkspaceLeaseMetadataValue] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            msg = "metadata keys must be non-empty strings"
            raise WorkspaceLeaseRegistryError(msg)
        normalized_key = key.strip()
        if len(normalized_key) > _MAX_METADATA_KEY_LENGTH:
            msg = f"metadata keys must be at most {_MAX_METADATA_KEY_LENGTH} characters"
            raise WorkspaceLeaseRegistryError(msg)
        if isinstance(value, str):
            if len(value) > _MAX_METADATA_VALUE_LENGTH:
                msg = f"metadata values must be at most {_MAX_METADATA_VALUE_LENGTH} characters"
                raise WorkspaceLeaseRegistryError(msg)
            normalized[normalized_key] = value
        elif isinstance(value, bool | int | float) or value is None:
            normalized[normalized_key] = value
        else:
            msg = "metadata values must be scalar JSON values"
            raise WorkspaceLeaseRegistryError(msg)
    return normalized


def _record_from_payload(
    lease_id: str,
    payload: Mapping[object, object],
    *,
    storage_root: Path,
) -> WorkspaceLeaseRecord:
    payload_lease_id = payload.get("lease_id", lease_id)
    if payload_lease_id != lease_id:
        msg = "lease_id mismatch"
        raise WorkspaceLeaseRegistryError(msg)
    workspace_root = payload.get("workspace_root")
    if not isinstance(workspace_root, str):
        msg = "workspace_root must be a string"
        raise WorkspaceLeaseRegistryError(msg)
    status = payload.get("status")
    if status not in ("active", "released", "expired"):
        msg = "status must be active, released, or expired"
        raise WorkspaceLeaseRegistryError(msg)
    created_at = _payload_number(payload.get("created_at"), "created_at")
    updated_at = _payload_number(payload.get("updated_at"), "updated_at")
    expires_at = _payload_optional_number(payload.get("expires_at"), "expires_at")
    released_at = _payload_optional_number(payload.get("released_at"), "released_at")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return WorkspaceLeaseRecord(
        lease_id=_validate_lease_id(lease_id),
        workspace_root=_resolve_workspace_root(Path(workspace_root), storage_root=storage_root),
        owner_kind=_validate_limited_text(payload.get("owner_kind"), "owner_kind", max_length=_MAX_OWNER_KIND_LENGTH),
        owner_id=_validate_limited_text(payload.get("owner_id"), "owner_id", max_length=_MAX_OWNER_ID_LENGTH),
        purpose=_validate_optional_limited_text(payload.get("purpose"), "purpose", max_length=_MAX_PURPOSE_LENGTH),
        status=cast("WorkspaceLeaseStatus", status),
        metadata=_validate_metadata(cast("Mapping[str, object]", metadata)),
        created_at=created_at,
        updated_at=updated_at,
        expires_at=expires_at,
        released_at=released_at,
    )


def _payload_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{field_name} must be a number"
        raise WorkspaceLeaseRegistryError(msg)
    return float(value)


def _payload_optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _payload_number(value, field_name)


def _lease_with_effective_status(record: WorkspaceLeaseRecord, *, now: float) -> WorkspaceLeaseRecord:
    if record.status == "active" and record.expires_at is not None and record.expires_at <= now:
        return replace(record, status="expired", updated_at=now)
    return record


def _record_to_payload(record: WorkspaceLeaseRecord) -> WorkspaceLeaseRecordPayload:
    return {
        "lease_id": record.lease_id,
        "workspace_root": str(record.workspace_root),
        "owner_kind": record.owner_kind,
        "owner_id": record.owner_id,
        "purpose": record.purpose,
        "status": record.status,
        "metadata": dict(record.metadata),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "expires_at": record.expires_at,
        "released_at": record.released_at,
    }


def _require_record(records: Mapping[str, WorkspaceLeaseRecord], lease_id: str) -> WorkspaceLeaseRecord:
    try:
        return records[lease_id]
    except KeyError:
        msg = f"workspace lease not found: {lease_id}"
        raise WorkspaceLeaseRegistryError(msg) from None