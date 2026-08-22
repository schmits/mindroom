"""Metadata-only links between durable handoff artifacts and workspace leases.

This module validates references between :mod:`mindroom.handoff_artifacts` and
:mod:`mindroom.workspace_leases` records without reading, fetching, cloning, or
materializing any referenced content. Linked records remain evidence only unless
their own closed trust/authority labels say otherwise; repo-authored
instructions and design references are always treated as non-authoritative and
non-materializable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from typing import Any, Self

from mindroom.handoff_artifacts import (
    HandoffArtifact,
    HandoffArtifactValidationError,
    HandoffAuthority,
    HandoffSourceClassification,
    HandoffTrust,
)
from mindroom.workspace_leases import (
    WorkspaceLease,
    WorkspaceLeaseAuthority,
    WorkspaceLeaseSourceClassification,
    WorkspaceLeaseTrust,
    WorkspaceLeaseValidationError,
)


class ArtifactLeaseLinkValidationError(ValueError):
    """Raised when artifact/lease references are ambiguous or inconsistent."""


class LinkedRecordAuthority(StrEnum):
    """Authority a validated artifact/lease link may exert by itself."""

    EVIDENCE = "evidence"
    NON_AUTHORITATIVE = "non_authoritative"


_SCHEMA_FIELDS = frozenset({"artifact", "lease", "artifact_ref", "lease_ref", "authority", "sha256"})
_HANDOFF_ARTIFACT_SCHEMA_FIELDS = frozenset({
    "artifact_id",
    "classification",
    "producer",
    "consumer",
    "trust",
    "authority",
    "refs",
    "manifest",
    "sha256",
    "expires_at",
    "lease_ref",
    "materialize_allowed",
    "metadata",
})
_WORKSPACE_LEASE_SCHEMA_FIELDS = frozenset({
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
    "artifact_refs",
    "handoff_refs",
    "provenance",
    "metadata",
    "sha256",
})
_STRUCTURAL_HASH_FIELDS = ("artifact", "lease", "artifact_ref", "lease_ref", "authority")
_NON_AUTHORITATIVE_HANDOFF_CLASSES = {
    HandoffSourceClassification.DESIGN_REFERENCE,
    HandoffSourceClassification.UNTRUSTED_REPO_CONTENT,
}
_NON_AUTHORITATIVE_LEASE_CLASSES = {
    WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
    WorkspaceLeaseSourceClassification.UNTRUSTED_REPO_CONTENT,
}
# Artifact/lease classification compatibility is intentionally total over
# HandoffSourceClassification.  Do not use dict.get() here: a newly added
# artifact class must fail closed until this table explicitly maps or rejects
# it.  Lease-side OPERATOR_REQUEST is intentionally unsupported for
# artifact↔lease links because it is a request provenance class, not durable
# handoff artifact evidence.
_CLASSIFICATION_COMPATIBILITY: dict[
    HandoffSourceClassification, frozenset[WorkspaceLeaseSourceClassification],
] = {
    HandoffSourceClassification.DESIGN_REFERENCE: frozenset({
        WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
    }),
    HandoffSourceClassification.TARGET_REPO: frozenset({
        WorkspaceLeaseSourceClassification.TARGET_REPO,
    }),
    HandoffSourceClassification.RUNTIME_CONFIG: frozenset({
        WorkspaceLeaseSourceClassification.RUNTIME_RECORD,
    }),
    HandoffSourceClassification.ARTIFACT: frozenset({
        WorkspaceLeaseSourceClassification.ARTIFACT,
        WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT,
    }),
    HandoffSourceClassification.UNTRUSTED_REPO_CONTENT: frozenset({
        WorkspaceLeaseSourceClassification.UNTRUSTED_REPO_CONTENT,
    }),
    HandoffSourceClassification.IMPLEMENTATION_EVIDENCE: frozenset({
        WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT,
    }),
    HandoffSourceClassification.REVIEW_FINDING: frozenset({
        WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT,
    }),
}
_UNSUPPORTED_LINK_LEASE_CLASSIFICATIONS = frozenset({
    WorkspaceLeaseSourceClassification.OPERATOR_REQUEST,
})
_SUPPORTED_LINK_LEASE_CLASSIFICATIONS = frozenset().union(*_CLASSIFICATION_COMPATIBILITY.values())
if set(_CLASSIFICATION_COMPATIBILITY) != set(HandoffSourceClassification):
    msg = "artifact lease link classification compatibility is not total over handoff classifications"
    raise RuntimeError(msg)
if set(WorkspaceLeaseSourceClassification) != (
    _SUPPORTED_LINK_LEASE_CLASSIFICATIONS | _UNSUPPORTED_LINK_LEASE_CLASSIFICATIONS
):
    msg = "artifact lease link lease classification support/rejection table is not total"
    raise RuntimeError(msg)
if _SUPPORTED_LINK_LEASE_CLASSIFICATIONS & _UNSUPPORTED_LINK_LEASE_CLASSIFICATIONS:
    msg = "artifact lease link lease classification support table overlaps unsupported classes"
    raise RuntimeError(msg)
_TRUST_PAIRS = {
    HandoffTrust.TRUSTED_RUNTIME: WorkspaceLeaseTrust.TRUSTED_RUNTIME,
    HandoffTrust.VERIFIED_ARTIFACT: WorkspaceLeaseTrust.VERIFIED_METADATA,
    HandoffTrust.UNTRUSTED: WorkspaceLeaseTrust.UNTRUSTED,
}
_AUTHORITY_PAIRS = {
    HandoffAuthority.AUTHORITATIVE: WorkspaceLeaseAuthority.AUTHORITATIVE,
    HandoffAuthority.EVIDENCE: WorkspaceLeaseAuthority.EVIDENCE,
    HandoffAuthority.NON_AUTHORITATIVE: WorkspaceLeaseAuthority.NON_AUTHORITATIVE,
}


def _expected_lease_classifications(
    artifact_classification: HandoffSourceClassification,
) -> frozenset[WorkspaceLeaseSourceClassification]:
    try:
        expected_classifications = _CLASSIFICATION_COMPATIBILITY[artifact_classification]
    except KeyError as exc:
        msg = f"unsupported handoff artifact classification for lease link: {artifact_classification.value}"
        raise ArtifactLeaseLinkValidationError(msg) from exc
    if not expected_classifications:
        msg = f"unsupported handoff artifact classification for lease link: {artifact_classification.value}"
        raise ArtifactLeaseLinkValidationError(msg)
    return expected_classifications


def _validate_classifications(artifact: HandoffArtifact, lease: WorkspaceLease) -> None:
    expected_classifications = _expected_lease_classifications(artifact.classification)
    if lease.classification in expected_classifications:
        return
    expected_values = ", ".join(sorted(classification.value for classification in expected_classifications))
    msg = (
        "linked artifact and lease source classifications are inconsistent "
        f"(expected lease classification: {expected_values})"
    )
    raise ArtifactLeaseLinkValidationError(msg)


def _validate_trust_and_authority(artifact: HandoffArtifact, lease: WorkspaceLease) -> None:
    expected_trust = _TRUST_PAIRS[artifact.trust]
    if lease.trust is not expected_trust:
        msg = "linked artifact and lease trust labels are inconsistent"
        raise ArtifactLeaseLinkValidationError(msg)
    expected_authority = _AUTHORITY_PAIRS[artifact.authority]
    if lease.authority is not expected_authority:
        msg = "linked artifact and lease authority labels are inconsistent"
        raise ArtifactLeaseLinkValidationError(msg)


def _validate_non_authoritative_materialization(link: ArtifactLeaseLink) -> None:
    if (
        link.artifact.classification not in _NON_AUTHORITATIVE_HANDOFF_CLASSES
        and link.lease.classification not in _NON_AUTHORITATIVE_LEASE_CLASSES
    ):
        return
    if link.artifact.materialize_allowed:
        msg = "design reference and repo-authored instruction links must not allow materialization"
        raise ArtifactLeaseLinkValidationError(msg)
    if link.authority is not LinkedRecordAuthority.NON_AUTHORITATIVE:
        msg = "design reference and repo-authored instruction links are non-authoritative"
        raise ArtifactLeaseLinkValidationError(msg)


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"artifact lease link {field_name} must be a non-empty string"
        raise ArtifactLeaseLinkValidationError(msg)
    if value != value.strip() or any(char.isspace() for char in value):
        msg = f"artifact lease link {field_name} must not contain whitespace"
        raise ArtifactLeaseLinkValidationError(msg)
    return value


def _require_ref(value: object, *, field_name: str) -> str:
    ref = _require_non_empty_string(value, field_name=field_name)
    if "://" not in ref:
        msg = f"artifact lease link {field_name} must be an opaque URI reference"
        raise ArtifactLeaseLinkValidationError(msg)
    return ref


def _require_exact_mapping_fields(
    value: object,
    *,
    field_name: str,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"artifact lease link {field_name} must be a mapping"
        raise ArtifactLeaseLinkValidationError(msg)
    missing_keys = expected_fields - set(value)
    if missing_keys:
        msg = f"artifact lease link {field_name} is missing required keys: {', '.join(sorted(missing_keys))}"
        raise ArtifactLeaseLinkValidationError(msg)
    extra_keys = set(value) - expected_fields
    if extra_keys:
        msg = f"artifact lease link {field_name} has unknown keys: {', '.join(sorted(extra_keys))}"
        raise ArtifactLeaseLinkValidationError(msg)
    return value


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _link_authority(artifact: HandoffArtifact, lease: WorkspaceLease) -> LinkedRecordAuthority:
    if (
        artifact.classification in _NON_AUTHORITATIVE_HANDOFF_CLASSES
        or lease.classification in _NON_AUTHORITATIVE_LEASE_CLASSES
        or artifact.authority is HandoffAuthority.NON_AUTHORITATIVE
        or lease.authority is WorkspaceLeaseAuthority.NON_AUTHORITATIVE
    ):
        return LinkedRecordAuthority.NON_AUTHORITATIVE
    return LinkedRecordAuthority.EVIDENCE


@dataclass(frozen=True, slots=True)
class ArtifactLeaseLink:
    """Validated metadata-only artifact ↔ workspace-lease reference.

    The link records relationship metadata and a canonical integrity hash only.
    It does not grant access to, materialize, fetch, clone, or otherwise trust
    any referenced repository, artifact, workspace, or design reference.
    """

    artifact: HandoffArtifact
    lease: WorkspaceLease
    artifact_ref: str
    lease_ref: str
    authority: LinkedRecordAuthority
    sha256: str

    @classmethod
    def create(
        cls,
        *,
        artifact: HandoffArtifact,
        lease: WorkspaceLease,
        artifact_ref: str | None = None,
        lease_ref: str | None = None,
    ) -> Self:
        """Create a validated link and attach its canonical integrity hash."""
        base = cls(
            artifact=artifact,
            lease=lease,
            artifact_ref=artifact_ref or artifact.refs[0],
            lease_ref=lease_ref or f"lease://{lease.lease_id}",
            authority=_link_authority(artifact, lease),
            sha256="pending",
        )
        link = replace(base, sha256=base.integrity_hash())
        link.validate()
        return link

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> Self:
        """Build and validate a link from JSON-like data, failing closed."""
        if not isinstance(payload, dict):
            msg = "artifact lease link payload must be a mapping"
            raise ArtifactLeaseLinkValidationError(msg)
        missing_keys = _SCHEMA_FIELDS - set(payload)
        if missing_keys:
            msg = f"artifact lease link payload is missing required keys: {', '.join(sorted(missing_keys))}"
            raise ArtifactLeaseLinkValidationError(msg)
        extra_keys = set(payload) - _SCHEMA_FIELDS
        if extra_keys:
            msg = f"artifact lease link payload has unknown keys: {', '.join(sorted(extra_keys))}"
            raise ArtifactLeaseLinkValidationError(msg)
        artifact_payload = _require_exact_mapping_fields(
            payload["artifact"],
            field_name="artifact",
            expected_fields=_HANDOFF_ARTIFACT_SCHEMA_FIELDS,
        )
        lease_payload = _require_exact_mapping_fields(
            payload["lease"],
            field_name="lease",
            expected_fields=_WORKSPACE_LEASE_SCHEMA_FIELDS,
        )
        try:
            artifact = HandoffArtifact.from_mapping(artifact_payload)
            lease = WorkspaceLease.from_mapping(lease_payload)
        except (HandoffArtifactValidationError, WorkspaceLeaseValidationError) as exc:
            msg = f"artifact lease link contains invalid linked record: {exc}"
            raise ArtifactLeaseLinkValidationError(msg) from exc
        authority_value = payload["authority"]
        if not isinstance(authority_value, str):
            msg = "artifact lease link authority is required"
            raise ArtifactLeaseLinkValidationError(msg)
        try:
            authority = LinkedRecordAuthority(authority_value)
        except ValueError as exc:
            msg = f"artifact lease link authority has invalid value {authority_value!r}"
            raise ArtifactLeaseLinkValidationError(msg) from exc
        link = cls(
            artifact=artifact,
            lease=lease,
            artifact_ref=_require_ref(payload["artifact_ref"], field_name="artifact_ref"),
            lease_ref=_require_ref(payload["lease_ref"], field_name="lease_ref"),
            authority=authority,
            sha256=_require_non_empty_string(payload["sha256"], field_name="sha256"),
        )
        link.validate()
        return link

    def structural_mapping(self) -> dict[str, object]:
        """Return the link payload covered by ``sha256``."""
        return {
            field_name: self.to_mapping(include_integrity=False)[field_name]
            for field_name in _STRUCTURAL_HASH_FIELDS
        }

    def integrity_hash(self) -> str:
        """Return the canonical SHA-256 over linked metadata and record hashes."""
        return sha256(_canonical_payload(self.structural_mapping())).hexdigest()

    def to_mapping(self, *, include_integrity: bool = True) -> dict[str, object]:
        """Return a JSON-safe representation with exact, fail-closed keys."""
        payload: dict[str, object] = {
            "artifact": self.artifact.to_mapping(),
            "lease": self.lease.to_mapping(),
            "artifact_ref": self.artifact_ref,
            "lease_ref": self.lease_ref,
            "authority": self.authority.value,
        }
        if include_integrity:
            payload["sha256"] = self.sha256
        return payload

    def validate(self) -> None:
        """Validate linked record integrity, labels, and cross references."""
        self.artifact.validate()
        self.lease.validate()
        _require_ref(self.artifact_ref, field_name="artifact_ref")
        _require_ref(self.lease_ref, field_name="lease_ref")
        if self.artifact.sha256 != self.artifact.integrity_hash():
            msg = "linked handoff artifact integrity hash is invalid"
            raise ArtifactLeaseLinkValidationError(msg)
        if self.lease.sha256 != self.lease.integrity_hash():
            msg = "linked workspace lease integrity hash is invalid"
            raise ArtifactLeaseLinkValidationError(msg)
        if self.artifact.lease_ref != self.lease_ref:
            msg = "handoff artifact lease_ref must match linked lease_ref"
            raise ArtifactLeaseLinkValidationError(msg)
        if self.artifact_ref not in self.artifact.refs:
            msg = "handoff artifact must reference the linked artifact_ref"
            raise ArtifactLeaseLinkValidationError(msg)
        if self.artifact_ref not in self.lease.artifact_refs and self.artifact_ref not in self.lease.handoff_refs:
            msg = "workspace lease must reference the linked artifact_ref"
            raise ArtifactLeaseLinkValidationError(msg)
        _validate_classifications(self.artifact, self.lease)
        _validate_trust_and_authority(self.artifact, self.lease)
        if self.authority is not _link_authority(self.artifact, self.lease):
            msg = "artifact lease link authority does not match linked records"
            raise ArtifactLeaseLinkValidationError(msg)
        _validate_non_authoritative_materialization(self)
        if self.sha256 != self.integrity_hash():
            msg = "artifact lease link integrity hash is missing or invalid"
            raise ArtifactLeaseLinkValidationError(msg)