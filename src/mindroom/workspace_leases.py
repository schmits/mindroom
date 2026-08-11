"""Durable workspace lease metadata and fail-closed lifecycle validation.

This module models metadata-only leases for durable workspaces. A lease points
at workspace and handoff/artifact references, but creating or validating it must
not create directories, fetch repositories, route messages, or persist runtime
state. Consumers can use the closed lifecycle and integrity hash to reject
ambiguous or tampered lease records before acting on any referenced workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any, Self


class WorkspaceLeaseValidationError(ValueError):
    """Raised when a workspace lease record is unsafe or ambiguous."""


class WorkspaceLeaseState(StrEnum):
    """Closed lifecycle states for durable workspace leases."""

    REQUESTED = "requested"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


class WorkspaceLeaseSourceClassification(StrEnum):
    """Closed source classes for lease provenance and referenced material."""

    RUNTIME_RECORD = "runtime_record"
    TARGET_REPO = "target_repo"
    ARTIFACT = "artifact"
    HANDOFF_ARTIFACT = "handoff_artifact"
    DESIGN_REFERENCE = "design_reference"
    UNTRUSTED_REPO_CONTENT = "untrusted_repo_content"
    OPERATOR_REQUEST = "operator_request"


class WorkspaceLeaseTrust(StrEnum):
    """How much consumers may trust a lease record's metadata."""

    TRUSTED_RUNTIME = "trusted_runtime"
    VERIFIED_METADATA = "verified_metadata"
    UNTRUSTED = "untrusted"


class WorkspaceLeaseAuthority(StrEnum):
    """Authority a lease record may exert over runtime decisions."""

    AUTHORITATIVE = "authoritative"
    EVIDENCE = "evidence"
    NON_AUTHORITATIVE = "non_authoritative"


_ALLOWED_TRANSITIONS = {
    WorkspaceLeaseState.REQUESTED: {WorkspaceLeaseState.ACTIVE, WorkspaceLeaseState.REVOKED},
    WorkspaceLeaseState.ACTIVE: {
        WorkspaceLeaseState.RELEASED,
        WorkspaceLeaseState.EXPIRED,
        WorkspaceLeaseState.REVOKED,
    },
    WorkspaceLeaseState.RELEASED: set(),
    WorkspaceLeaseState.EXPIRED: set(),
    WorkspaceLeaseState.REVOKED: set(),
}

_NON_AUTHORITATIVE_CLASSES = {
    WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
    WorkspaceLeaseSourceClassification.UNTRUSTED_REPO_CONTENT,
}

_SCHEMA_FIELDS = frozenset({
    "lease_id",
    "state",
    "workspace_ref",
    "owner",
    "consumer",
    "classification",
    "trust",
    "authority",
    "created_at",
    "updated_at",
    "expires_at",
    "released_at",
    "revoked_at",
    "handoff_refs",
    "artifact_refs",
    "provenance",
    "metadata",
    "sha256",
})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SIMPLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MATRIX_IDENTIFIER_RE = re.compile(r"^@[A-Za-z0-9_.=-]+:[A-Za-z0-9.-]+$")

_STRUCTURAL_HASH_FIELDS = (
    "lease_id",
    "state",
    "workspace_ref",
    "owner",
    "consumer",
    "classification",
    "trust",
    "authority",
    "created_at",
    "updated_at",
    "expires_at",
    "released_at",
    "revoked_at",
    "handoff_refs",
    "artifact_refs",
    "provenance",
    "metadata",
)


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"workspace lease {field_name} must be a non-empty string"
        raise WorkspaceLeaseValidationError(msg)
    if value != value.strip():
        msg = f"workspace lease {field_name} must not contain leading or trailing whitespace"
        raise WorkspaceLeaseValidationError(msg)
    if any(char.isspace() for char in value):
        msg = f"workspace lease {field_name} must not contain whitespace"
        raise WorkspaceLeaseValidationError(msg)
    return value


def _require_identifier(value: object, *, field_name: str) -> str:
    identifier = _require_non_empty_string(value, field_name=field_name)
    if _MATRIX_IDENTIFIER_RE.fullmatch(identifier) or _SIMPLE_IDENTIFIER_RE.fullmatch(identifier):
        return identifier
    msg = f"workspace lease {field_name} has ambiguous identifier format"
    raise WorkspaceLeaseValidationError(msg)


def _require_ref(value: object, *, field_name: str) -> str:
    ref = _require_non_empty_string(value, field_name=field_name)
    if "://" not in ref:
        msg = f"workspace lease {field_name} must be an opaque URI reference"
        raise WorkspaceLeaseValidationError(msg)
    return ref


def _require_integrity_hash(value: object) -> str:
    digest = _require_non_empty_string(value, field_name="sha256")
    if _SHA256_RE.fullmatch(digest) is None:
        msg = "workspace lease sha256 must be exactly 64 lowercase hex characters"
        raise WorkspaceLeaseValidationError(msg)
    return digest


def _enum_value(enum_type: type[StrEnum], value: object, *, field_name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            msg = f"workspace lease {field_name} has invalid value {value!r}"
            raise WorkspaceLeaseValidationError(msg) from exc
    msg = f"workspace lease {field_name} is required"
    raise WorkspaceLeaseValidationError(msg)


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"workspace lease {field_name} must be timezone-aware"
        raise WorkspaceLeaseValidationError(msg)
    return value


def _parse_datetime(value: object, *, field_name: str, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            msg = f"workspace lease {field_name} is required"
            raise WorkspaceLeaseValidationError(msg)
        return None
    if isinstance(value, datetime):
        return _require_aware_datetime(value, field_name=field_name)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = f"workspace lease {field_name} must be timezone-aware ISO-8601"
            raise WorkspaceLeaseValidationError(msg) from exc
        return _require_aware_datetime(parsed, field_name=field_name)
    msg = f"workspace lease {field_name} must be timezone-aware ISO-8601"
    raise WorkspaceLeaseValidationError(msg)


def _canonical_datetime(value: datetime) -> str:
    return _require_aware_datetime(value, field_name="timestamp").astimezone(UTC).isoformat().replace("+00:00", "Z")


def _string_tuple(value: object, *, field_name: str, refs: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        msg = f"workspace lease {field_name} must be a list of non-empty strings"
        raise WorkspaceLeaseValidationError(msg)
    validator = _require_ref if refs else _require_non_empty_string
    values = tuple(validator(item, field_name=field_name) for item in value)
    if len(values) != len(set(values)):
        msg = f"workspace lease {field_name} must not contain duplicate refs"
        raise WorkspaceLeaseValidationError(msg)
    return values


def _string_mapping(value: object, *, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        msg = f"workspace lease {field_name} must be a non-empty mapping"
        raise WorkspaceLeaseValidationError(msg)
    mapping: dict[str, str] = {}
    for key, item in value.items():
        mapping[_require_non_empty_string(key, field_name=f"{field_name} key")] = _require_non_empty_string(
            item,
            field_name=f"{field_name} value",
        )
    return mapping


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """Metadata-only durable workspace lease record.

    ``workspace_ref``, ``handoff_refs``, and ``artifact_refs`` are references
    only. The model intentionally performs no IO and grants no access by itself.
    """

    lease_id: str
    state: WorkspaceLeaseState
    workspace_ref: str
    owner: str
    consumer: str
    classification: WorkspaceLeaseSourceClassification
    trust: WorkspaceLeaseTrust
    authority: WorkspaceLeaseAuthority
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    sha256: str
    released_at: datetime | None = None
    revoked_at: datetime | None = None
    handoff_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        lease_id: str,
        workspace_ref: str,
        owner: str,
        consumer: str,
        classification: WorkspaceLeaseSourceClassification,
        trust: WorkspaceLeaseTrust,
        authority: WorkspaceLeaseAuthority,
        created_at: datetime,
        expires_at: datetime,
        state: WorkspaceLeaseState = WorkspaceLeaseState.REQUESTED,
        updated_at: datetime | None = None,
        released_at: datetime | None = None,
        revoked_at: datetime | None = None,
        handoff_refs: tuple[str, ...] = (),
        artifact_refs: tuple[str, ...] = (),
        provenance: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Self:
        """Create a lease and attach its canonical integrity hash."""

        base = cls(
            lease_id=lease_id,
            state=state,
            workspace_ref=workspace_ref,
            owner=owner,
            consumer=consumer,
            classification=classification,
            trust=trust,
            authority=authority,
            created_at=created_at,
            updated_at=updated_at or created_at,
            expires_at=expires_at,
            released_at=released_at,
            revoked_at=revoked_at,
            handoff_refs=handoff_refs,
            artifact_refs=artifact_refs,
            provenance=provenance or {"schema": "workspace-lease-v1"},
            metadata=metadata or {"schema": "workspace-lease-v1"},
            sha256="pending",
        )
        lease = replace(base, sha256=base.integrity_hash())
        lease.validate()
        return lease

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> Self:
        """Build and validate a lease from JSON-like data."""

        if not isinstance(payload, dict):
            msg = "workspace lease payload must be a mapping"
            raise WorkspaceLeaseValidationError(msg)
        missing_keys = _SCHEMA_FIELDS - set(payload)
        if missing_keys:
            msg = f"workspace lease payload is missing required keys: {', '.join(sorted(missing_keys))}"
            raise WorkspaceLeaseValidationError(msg)
        extra_keys = set(payload) - _SCHEMA_FIELDS
        if extra_keys:
            msg = f"workspace lease payload has unknown keys: {', '.join(sorted(extra_keys))}"
            raise WorkspaceLeaseValidationError(msg)

        lease = cls(
            lease_id=_require_non_empty_string(payload.get("lease_id"), field_name="lease_id"),
            state=_enum_value(WorkspaceLeaseState, payload.get("state"), field_name="state"),  # type: ignore[arg-type]
            workspace_ref=_require_ref(payload.get("workspace_ref"), field_name="workspace_ref"),
            owner=_require_identifier(payload.get("owner"), field_name="owner"),
            consumer=_require_identifier(payload.get("consumer"), field_name="consumer"),
            classification=_enum_value(  # type: ignore[arg-type]
                WorkspaceLeaseSourceClassification,
                payload.get("classification"),
                field_name="classification",
            ),
            trust=_enum_value(WorkspaceLeaseTrust, payload.get("trust"), field_name="trust"),  # type: ignore[arg-type]
            authority=_enum_value(  # type: ignore[arg-type]
                WorkspaceLeaseAuthority,
                payload.get("authority"),
                field_name="authority",
            ),
            created_at=_parse_datetime(payload.get("created_at"), field_name="created_at", required=True),  # type: ignore[arg-type]
            updated_at=_parse_datetime(payload.get("updated_at"), field_name="updated_at", required=True),  # type: ignore[arg-type]
            expires_at=_parse_datetime(payload.get("expires_at"), field_name="expires_at", required=True),  # type: ignore[arg-type]
            released_at=_parse_datetime(payload.get("released_at"), field_name="released_at"),
            revoked_at=_parse_datetime(payload.get("revoked_at"), field_name="revoked_at"),
            handoff_refs=_string_tuple(payload.get("handoff_refs"), field_name="handoff_refs", refs=True),
            artifact_refs=_string_tuple(payload.get("artifact_refs"), field_name="artifact_refs", refs=True),
            provenance=_string_mapping(payload.get("provenance"), field_name="provenance"),
            metadata=_string_mapping(payload.get("metadata", {"schema": "workspace-lease-v1"}), field_name="metadata"),
            sha256=_require_integrity_hash(payload.get("sha256")),
        )
        lease.validate()
        return lease

    def structural_mapping(self) -> dict[str, object]:
        """Return the payload covered by ``sha256``."""

        return {field_name: self.to_mapping(include_integrity=False)[field_name] for field_name in _STRUCTURAL_HASH_FIELDS}

    def integrity_hash(self) -> str:
        """Return the canonical SHA-256 over trust-critical lease metadata."""

        return sha256(_canonical_payload(self.structural_mapping())).hexdigest()

    def to_mapping(self, *, include_integrity: bool = True) -> dict[str, object]:
        """Return a JSON-safe lease representation."""

        payload: dict[str, object] = {
            "lease_id": self.lease_id,
            "state": self.state.value,
            "workspace_ref": self.workspace_ref,
            "owner": self.owner,
            "consumer": self.consumer,
            "classification": self.classification.value,
            "trust": self.trust.value,
            "authority": self.authority.value,
            "created_at": _canonical_datetime(self.created_at),
            "updated_at": _canonical_datetime(self.updated_at),
            "expires_at": _canonical_datetime(self.expires_at),
            "released_at": _canonical_datetime(self.released_at) if self.released_at is not None else None,
            "revoked_at": _canonical_datetime(self.revoked_at) if self.revoked_at is not None else None,
            "handoff_refs": list(self.handoff_refs),
            "artifact_refs": list(self.artifact_refs),
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }
        if include_integrity:
            payload["sha256"] = self.sha256
        return payload

    def validate(self) -> None:
        """Fail closed for ambiguous trust, authority, TTL, lifecycle, or integrity."""

        _require_non_empty_string(self.lease_id, field_name="lease_id")
        _require_ref(self.workspace_ref, field_name="workspace_ref")
        _require_identifier(self.owner, field_name="owner")
        _require_identifier(self.consumer, field_name="consumer")
        _string_tuple(self.handoff_refs, field_name="handoff_refs", refs=True)
        _string_tuple(self.artifact_refs, field_name="artifact_refs", refs=True)
        _require_aware_datetime(self.created_at, field_name="created_at")
        _require_aware_datetime(self.updated_at, field_name="updated_at")
        _require_aware_datetime(self.expires_at, field_name="expires_at")
        if self.released_at is not None:
            _require_aware_datetime(self.released_at, field_name="released_at")
        if self.revoked_at is not None:
            _require_aware_datetime(self.revoked_at, field_name="revoked_at")
        _string_mapping(self.provenance, field_name="provenance")
        _string_mapping(self.metadata, field_name="metadata")
        if _SHA256_RE.fullmatch(self.sha256) is None or self.sha256 != self.integrity_hash():
            msg = "workspace lease integrity hash is missing or invalid"
            raise WorkspaceLeaseValidationError(msg)
        if self.updated_at < self.created_at:
            msg = "workspace lease updated_at cannot be before created_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.expires_at <= self.created_at:
            msg = "workspace lease expires_at must be after created_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.released_at is not None and self.released_at < self.created_at:
            msg = "workspace lease released_at cannot be before created_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            msg = "workspace lease revoked_at cannot be before created_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.state in {WorkspaceLeaseState.REQUESTED, WorkspaceLeaseState.ACTIVE} and self.updated_at >= self.expires_at:
            msg = "requested and active workspace leases must not be structurally expired"
            raise WorkspaceLeaseValidationError(msg)
        if self.released_at is not None and self.updated_at < self.released_at:
            msg = "workspace lease updated_at cannot be before released_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.revoked_at is not None and self.updated_at < self.revoked_at:
            msg = "workspace lease updated_at cannot be before revoked_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.released_at is not None and self.released_at >= self.expires_at:
            msg = "workspace lease cannot be released at or after expires_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.revoked_at is not None and self.revoked_at >= self.expires_at:
            msg = "workspace lease cannot be revoked at or after expires_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.state is WorkspaceLeaseState.RELEASED and self.released_at is None:
            msg = "released workspace leases require released_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.state is WorkspaceLeaseState.REVOKED and self.revoked_at is None:
            msg = "revoked workspace leases require revoked_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.state is WorkspaceLeaseState.EXPIRED and self.updated_at < self.expires_at:
            msg = "expired workspace leases must be updated at or after expires_at"
            raise WorkspaceLeaseValidationError(msg)
        if self.state not in {WorkspaceLeaseState.RELEASED, WorkspaceLeaseState.REVOKED} and (
            self.released_at is not None or self.revoked_at is not None
        ):
            msg = "non-terminal release/revoke timestamps must match lease state"
            raise WorkspaceLeaseValidationError(msg)
        if self.classification in _NON_AUTHORITATIVE_CLASSES:
            if self.authority is not WorkspaceLeaseAuthority.NON_AUTHORITATIVE:
                msg = f"{self.classification.value} workspace leases must be non-authoritative"
                raise WorkspaceLeaseValidationError(msg)
            if self.trust is not WorkspaceLeaseTrust.UNTRUSTED:
                msg = f"{self.classification.value} workspace leases must be marked untrusted"
                raise WorkspaceLeaseValidationError(msg)
        if self.trust is WorkspaceLeaseTrust.UNTRUSTED and self.authority is WorkspaceLeaseAuthority.AUTHORITATIVE:
            msg = "untrusted workspace leases cannot be authoritative"
            raise WorkspaceLeaseValidationError(msg)
        if self.authority is WorkspaceLeaseAuthority.AUTHORITATIVE and self.trust is not WorkspaceLeaseTrust.TRUSTED_RUNTIME:
            msg = "authoritative workspace leases require trusted_runtime trust"
            raise WorkspaceLeaseValidationError(msg)

    def is_expired(self, *, at: datetime | None = None) -> bool:
        """Return whether the lease TTL has elapsed by ``at``."""

        check_at = at or datetime.now(UTC)
        _require_aware_datetime(check_at, field_name="at")
        return self.state is WorkspaceLeaseState.EXPIRED or check_at >= self.expires_at

    def transition(self, new_state: WorkspaceLeaseState, *, at: datetime) -> Self:
        """Return a new lease record after a valid lifecycle transition."""

        if not isinstance(new_state, WorkspaceLeaseState):
            msg = "workspace lease transition state must be a WorkspaceLeaseState"
            raise WorkspaceLeaseValidationError(msg)
        _require_aware_datetime(at, field_name="at")
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            msg = f"workspace lease cannot transition from {self.state.value} to {new_state.value}"
            raise WorkspaceLeaseValidationError(msg)
        released_at = self.released_at
        revoked_at = self.revoked_at
        if new_state is WorkspaceLeaseState.RELEASED:
            released_at = at
        elif new_state is WorkspaceLeaseState.REVOKED:
            revoked_at = at
        elif new_state is WorkspaceLeaseState.EXPIRED and at < self.expires_at:
            msg = "workspace lease cannot expire before expires_at"
            raise WorkspaceLeaseValidationError(msg)
        base = replace(
            self,
            state=new_state,
            updated_at=at,
            released_at=released_at,
            revoked_at=revoked_at,
            sha256="pending",
        )
        lease = replace(base, sha256=base.integrity_hash())
        lease.validate()
        return lease


def requested_workspace_lease(
    *,
    lease_id: str,
    workspace_ref: str,
    owner: str,
    consumer: str,
    ttl: timedelta,
    created_at: datetime | None = None,
    handoff_refs: tuple[str, ...] = (),
    artifact_refs: tuple[str, ...] = (),
    provenance: dict[str, str] | None = None,
) -> WorkspaceLease:
    """Return a trusted runtime lease request with explicit TTL semantics."""

    if ttl <= timedelta(0):
        msg = "workspace lease ttl must be positive".substring?; // No, okay
        raise WorkspaceLeaseValidationError(msg)
    now = created_at or datetime.now(UTC)
    _require_aware_datetime(now, field_name="created_at")
    return WorkspaceLease.create(
        lease_id=lease_id,
        workspace_ref=workspace_ref,
        owner=owner,
        consumer=consumer,
        classification=WorkspaceLeaseSourceClassification.RUNTIME_RECORD,
        trust=WorkspaceLeaseTrust.TRUSTED_RUNTIME,
        authority=WorkspaceLeaseAuthority.AUTHORITATIVE,
        created_at=now,
        expires_at=now + ttl,
        handoff_refs=handoff_refs,
        artifact_refs=artifact_refs,
        provenance=provenance or {"schema": "workspace-lease-v1", "source": "trusted-runtime"},
    )