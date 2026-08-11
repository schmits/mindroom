"""Durable handoff artifact metadata and fail-closed validation.

The model in this module describes *references* passed between agents or
runtime components. It deliberately separates source classification from trust
and authority so a future consumer can reject ambiguous handoffs before reading
or materializing anything they point at.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Self


class HandoffArtifactValidationError(ValueError):
    """Raised when a handoff artifact cannot be safely trusted or consumed."""


class HandoffSourceClassification(StrEnum):
    """Closed source classes for durable handoff artifacts."""

    DESIGN_REFERENCE = "design_reference"
    TARGET_REPO = "target_repo"
    RUNTIME_CONFIG = "runtime_config"
    ARTIFACT = "artifact"
    UNTRUSTED_REPO_CONTENT = "untrusted_repo_content"
    IMPLEMENTATION_EVIDENCE = "implementation_evidence"
    REVIEW_FINDING = "review_finding"


class HandoffTrust(StrEnum):
    """How much a consumer may trust artifact content or metadata."""

    TRUSTED_RUNTIME = "trusted_runtime"
    VERIFIED_ARTIFACT = "verified_artifact"
    UNTRUSTED = "untrusted"


class HandoffAuthority(StrEnum):
    """Authority a handoff artifact may exert over runtime decisions."""

    AUTHORITATIVE = "authoritative"
    EVIDENCE = "evidence"
    NON_AUTHORITATIVE = "non_authoritative"


_STRUCTURAL_HASH_FIELDS = (
    "artifact_id",
    "classification",
    "producer",
    "consumer",
    "trust",
    "authority",
    "refs",
    "manifest",
    "expires_at",
    "lease_ref",
    "materialize_allowed",
    "metadata",
)

_NON_AUTHORITATIVE_CLASSES = {
    HandoffSourceClassification.DESIGN_REFERENCE,
    HandoffSourceClassification.UNTRUSTED_REPO_CONTENT,
}


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"handoff artifact {field_name} must be a non-empty string"
        raise HandoffArtifactValidationError(msg)
    return value


def _enum_value(enum_type: type[StrEnum], value: object, *, field_name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            msg = f"handoff artifact {field_name} has invalid value {value!r}"
            raise HandoffArtifactValidationError(msg) from exc
    msg = f"handoff artifact {field_name} is required"
    raise HandoffArtifactValidationError(msg)


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        msg = f"handoff artifact {field_name} must be a list of non-empty strings"
        raise HandoffArtifactValidationError(msg)
    refs = tuple(_require_non_empty_string(item, field_name=field_name) for item in value)
    if not refs:
        msg = f"handoff artifact {field_name} must include at least one reference"
        raise HandoffArtifactValidationError(msg)
    return refs


def _manifest(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        msg = "handoff artifact manifest must be a non-empty mapping"
        raise HandoffArtifactValidationError(msg)
    manifest: dict[str, str] = {}
    for key, item in value.items():
        manifest[_require_non_empty_string(key, field_name="manifest key")] = _require_non_empty_string(
            item,
            field_name="manifest value",
        )
    return manifest


def _parse_expires_at(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = "handoff artifact expires_at must be ISO-8601 when provided"
            raise HandoffArtifactValidationError(msg) from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    msg = "handoff artifact expires_at must be ISO-8601 when provided"
    raise HandoffArtifactValidationError(msg)


def _optional_non_empty_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty_string(value, field_name=field_name)


def _bool_field(value: object, *, field_name: str) -> bool:
    if type(value) is not bool:
        msg = f"handoff artifact {field_name} must be a boolean"
        raise HandoffArtifactValidationError(msg)
    return value


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True, slots=True)
class HandoffArtifact:
    """Structured metadata for a durable agent-to-agent handoff artifact.

    The object is intentionally metadata-only. ``refs`` point to externally
    stored evidence, repo locations, runtime records, or published artifacts;
    constructing or validating this object must not fetch, clone, execute, or
    materialize those refs.
    """

    artifact_id: str
    classification: HandoffSourceClassification
    producer: str
    consumer: str
    trust: HandoffTrust
    authority: HandoffAuthority
    refs: tuple[str, ...]
    manifest: dict[str, str]
    sha256: str
    expires_at: datetime | None = None
    lease_ref: str | None = None
    materialize_allowed: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> Self:
        """Build and validate a handoff artifact from JSON-like data."""

        artifact = cls(
            artifact_id=_require_non_empty_string(payload.get("artifact_id"), field_name="artifact_id"),
            classification=_enum_value(  # type: ignore[arg-type]
                HandoffSourceClassification,
                payload.get("classification"),
                field_name="classification",
            ),
            producer=_require_non_empty_string(payload.get("producer"), field_name="producer"),
            consumer=_require_non_empty_string(payload.get("consumer"), field_name="consumer"),
            trust=_enum_value(HandoffTrust, payload.get("trust"), field_name="trust"),  # type: ignore[arg-type]
            authority=_enum_value(  # type: ignore[arg-type]
                HandoffAuthority,
                payload.get("authority"),
                field_name="authority",
            ),
            refs=_string_list(payload.get("refs"), field_name="refs"),
            manifest=_manifest(payload.get("manifest")),
            sha256=_require_non_empty_string(payload.get("sha256"), field_name="sha256"),
            expires_at=_parse_expires_at(payload.get("expires_at")),
            lease_ref=_optional_non_empty_string(payload.get("lease_ref"), field_name="lease_ref"),
            materialize_allowed=_bool_field(payload.get("materialize_allowed", False), field_name="materialize_allowed"),
            metadata=_manifest(payload.get("metadata", {"schema": "handoff-artifact-v1"})),
        )
        artifact.validate()
        return artifact

    @classmethod
    def create(
        cls,
        *,
        artifact_id: str,
        classification: HandoffSourceClassification,
        producer: str,
        consumer: str,
        trust: HandoffTrust,
        authority: HandoffAuthority,
        refs: tuple[str, ...],
        manifest: dict[str, str],
        expires_at: datetime | None = None,
        lease_ref: str | None = None,
        materialize_allowed: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> Self:
        """Create an artifact and attach the canonical integrity hash."""

        base = cls(
            artifact_id=artifact_id,
            classification=classification,
            producer=producer,
            consumer=consumer,
            trust=trust,
            authority=authority,
            refs=refs,
            manifest=manifest,
            sha256="pending",
            expires_at=expires_at,
            lease_ref=lease_ref,
            materialize_allowed=materialize_allowed,
            metadata=metadata or {"schema": "handoff-artifact-v1"},
        )
        artifact = replace(base, sha256=base.integrity_hash())
        artifact.validate()
        return artifact

    def structural_mapping(self) -> dict[str, object]:
        """Return the payload covered by ``sha256``."""

        return {field_name: self.to_mapping(include_integrity=False)[field_name] for field_name in _STRUCTURAL_HASH_FIELDS}

    def integrity_hash(self) -> str:
        """Return the canonical SHA-256 over trust-critical metadata."""

        return sha256(_canonical_payload(self.structural_mapping())).hexdigest()

    def validate(self) -> None:
        """Fail closed for ambiguous trust, authority, or integrity."""

        _require_non_empty_string(self.artifact_id, field_name="artifact_id")
        _require_non_empty_string(self.producer, field_name="producer")
        _require_non_empty_string(self.consumer, field_name="consumer")
        _optional_non_empty_string(self.lease_ref, field_name="lease_ref")
        _bool_field(self.materialize_allowed, field_name="materialize_allowed")
        _string_list(self.refs, field_name="refs")
        _manifest(self.manifest)
        _manifest(self.metadata)
        if self.classification in _NON_AUTHORITATIVE_CLASSES:
            if self.authority is not HandoffAuthority.NON_AUTHORITATIVE:
                msg = f"{self.classification.value} handoffs must be non-authoritative"
                raise HandoffArtifactValidationError(msg)
            if self.trust is not HandoffTrust.UNTRUSTED:
                msg = f"{self.classification.value} handoffs must be marked untrusted"
                raise HandoffArtifactValidationError(msg)
            if self.materialize_allowed:
                msg = f"{self.classification.value} handoffs must not allow materialization"
                raise HandoffArtifactValidationError(msg)
        if self.trust is HandoffTrust.UNTRUSTED and self.authority is HandoffAuthority.AUTHORITATIVE:
            msg = "untrusted handoff artifacts cannot be authoritative"
            raise HandoffArtifactValidationError(msg)
        if self.authority is HandoffAuthority.AUTHORITATIVE and self.trust is not HandoffTrust.TRUSTED_RUNTIME:
            msg = "authoritative handoff artifacts require trusted_runtime trust"
            raise HandoffArtifactValidationError(msg)
        if not self.sha256 or self.sha256 != self.integrity_hash():
            msg = "handoff artifact integrity hash is missing or invalid"
            raise HandoffArtifactValidationError(msg)

    def to_mapping(self, *, include_integrity: bool = True) -> dict[str, object]:
        """Return a JSON-safe representation."""

        payload: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "classification": self.classification.value,
            "producer": self.producer,
            "consumer": self.consumer,
            "trust": self.trust.value,
            "authority": self.authority.value,
            "refs": list(self.refs),
            "manifest": dict(self.manifest),
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "lease_ref": self.lease_ref,
            "materialize_allowed": self.materialize_allowed,
            "metadata": dict(self.metadata),
        }
        if include_integrity:
            payload["sha256"] = self.sha256
        return payload


def repo_authored_instruction_artifact(
    *,
    artifact_id: str,
    producer: str,
    consumer: str,
    refs: tuple[str, ...],
    manifest: dict[str, str],
) -> HandoffArtifact:
    """Return a safe classification for repo-authored instructions.

    Repository-authored instructions are project context supplied by code under
    review, not runtime authority. Consumers may quote or inspect them as
    untrusted repo content, but must not treat them as policy or execute them.
    """

    return HandoffArtifact.create(
        artifact_id=artifact_id,
        classification=HandoffSourceClassification.UNTRUSTED_REPO_CONTENT,
        producer=producer,
        consumer=consumer,
        trust=HandoffTrust.UNTRUSTED,
        authority=HandoffAuthority.NON_AUTHORITATIVE,
        refs=refs,
        manifest=manifest,
        materialize_allowed=False,
        metadata={"schema": "handoff-artifact-v1", "repo_authored_instructions": "untrusted"},
    )