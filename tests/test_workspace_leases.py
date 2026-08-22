"""Fail-closed durable workspace lease metadata tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mindroom.workspace_leases import (
    WorkspaceLease,
    WorkspaceLeaseAuthority,
    WorkspaceLeaseSourceClassification,
    WorkspaceLeaseState,
    WorkspaceLeaseTrust,
    WorkspaceLeaseValidationError,
    requested_workspace_lease,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _lease() -> WorkspaceLease:
    return requested_workspace_lease(
        lease_id="lease-1",
        workspace_ref="workspace://durable/lease-1",
        owner="@mindroom_mind:example.test",
        consumer="@mindroom_toolsmith:example.test",
        ttl=timedelta(hours=2),
        created_at=NOW,
        handoff_refs=("handoff://artifact-1",),
        artifact_refs=("artifact://handoff-summary.json",),
    )


def test_requested_workspace_lease_has_ttl_owner_consumer_and_refs() -> None:
    lease = _lease()

    assert lease.state is WorkspaceLeaseState.REQUESTED
    assert lease.owner == "@mindroom_mind:example.test"
    assert lease.consumer == "@mindroom_toolsmith:example.test"
    assert lease.expires_at == NOW + timedelta(hours=2)
    assert lease.handoff_refs == ("handoff://artifact-1",)
    assert lease.artifact_refs == ("artifact://handoff-summary.json",)
    assert lease.authority is WorkspaceLeaseAuthority.AUTHORITATIVE
    assert lease.trust is WorkspaceLeaseTrust.TRUSTED_RUNTIME
    assert lease.is_expired(at=NOW + timedelta(hours=1)) is False
    assert lease.is_expired(at=NOW + timedelta(hours=2)) is True


def test_non_positive_ttl_fails_closed() -> None:
    with pytest.raises(WorkspaceLeaseValidationError, match="ttl must be positive"):
        requested_workspace_lease(
            lease_id="bad-ttl",
            workspace_ref="workspace://durable/bad-ttl",
            owner="owner",
            consumer="consumer",
            ttl=timedelta(0),
            created_at=NOW,
        )


def test_required_fields_and_closed_enums_are_validated() -> None:
    payload = _lease().to_mapping()
    payload["state"] = "paused"
    with pytest.raises(WorkspaceLeaseValidationError, match="state has invalid value"):
        WorkspaceLease.from_mapping(payload)

    payload = _lease().to_mapping()
    payload["owner"] = ""
    with pytest.raises(WorkspaceLeaseValidationError, match="owner must be a non-empty string"):
        WorkspaceLease.from_mapping(payload)


def test_design_reference_lease_is_non_authoritative_untrusted() -> None:
    with pytest.raises(WorkspaceLeaseValidationError, match="design_reference workspace leases must be non-authoritative"):
        WorkspaceLease.create(
            lease_id="design-ref-lease",
            state=WorkspaceLeaseState.REQUESTED,
            workspace_ref="workspace://durable/design-ref",
            owner="producer",
            consumer="consumer",
            classification=WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
            trust=WorkspaceLeaseTrust.UNTRUSTED,
            authority=WorkspaceLeaseAuthority.AUTHORITATIVE,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            provenance={"schema": "workspace-lease-v1", "source": "design-reference"},
        )


def test_authoritative_lease_requires_trusted_runtime() -> None:
    with pytest.raises(WorkspaceLeaseValidationError, match="authoritative workspace leases require trusted_runtime trust"):
        WorkspaceLease.create(
            lease_id="verified-but-authoritative",
            state=WorkspaceLeaseState.REQUESTED,
            workspace_ref="workspace://durable/verified",
            owner="producer",
            consumer="consumer",
            classification=WorkspaceLeaseSourceClassification.ARTIFACT,
            trust=WorkspaceLeaseTrust.VERIFIED_METADATA,
            authority=WorkspaceLeaseAuthority.AUTHORITATIVE,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            provenance={"schema": "workspace-lease-v1", "source": "artifact"},
        )


def test_integrity_hash_is_required_and_checked() -> None:
    payload = _lease().to_mapping()
    payload["consumer"] = "tampered"

    with pytest.raises(WorkspaceLeaseValidationError, match="integrity hash is missing or invalid"):
        WorkspaceLease.from_mapping(payload)


def test_lifecycle_transitions_update_state_and_integrity() -> None:
    active = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1))
    released = active.transition(WorkspaceLeaseState.RELEASED, at=NOW + timedelta(minutes=30))

    assert released.state is WorkspaceLeaseState.RELEASED
    assert released.released_at == NOW + timedelta(minutes=30)
    assert WorkspaceLease.from_mapping(released.to_mapping()) == released


def test_invalid_lifecycle_transitions_fail_closed() -> None:
    lease = _lease()

    with pytest.raises(WorkspaceLeaseValidationError, match="cannot transition from requested to released"):
        lease.transition(WorkspaceLeaseState.RELEASED, at=NOW + timedelta(minutes=1))

    active = lease.transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1))
    with pytest.raises(WorkspaceLeaseValidationError, match="cannot expire before expires_at"):
        active.transition(WorkspaceLeaseState.EXPIRED, at=NOW + timedelta(minutes=30))


def test_terminal_states_cannot_transition_again() -> None:
    active = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1))
    revoked = active.transition(WorkspaceLeaseState.REVOKED, at=NOW + timedelta(minutes=2))

    with pytest.raises(WorkspaceLeaseValidationError, match="cannot transition from revoked to active"):
        revoked.transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=3))


def test_expired_state_requires_updated_at_at_or_after_expiration() -> None:
    with pytest.raises(WorkspaceLeaseValidationError, match="expired workspace leases must be updated at or after expires_at"):
        WorkspaceLease.create(
            lease_id="premature-expired",
            state=WorkspaceLeaseState.EXPIRED,
            workspace_ref="workspace://durable/premature",
            owner="producer",
            consumer="consumer",
            classification=WorkspaceLeaseSourceClassification.RUNTIME_RECORD,
            trust=WorkspaceLeaseTrust.TRUSTED_RUNTIME,
            authority=WorkspaceLeaseAuthority.AUTHORITATIVE,
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=30),
            expires_at=NOW + timedelta(hours=1),
            provenance={"schema": "workspace-lease-v1", "source": "trusted-runtime"},
        )


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    without_hash = {k: v for k, v in payload.items() if k != "sha256"}
    lease = WorkspaceLease(
        lease_id=without_hash["lease_id"],  # type: ignore[arg-type]
        state=WorkspaceLeaseState(without_hash["state"]),  # type: ignore[arg-type]
        workspace_ref=without_hash["workspace_ref"],  # type: ignore[arg-type]
        owner=without_hash["owner"],  # type: ignore[arg-type]
        consumer=without_hash["consumer"],  # type: ignore[arg-type]
        classification=WorkspaceLeaseSourceClassification(without_hash["classification"]),  # type: ignore[arg-type]
        trust=WorkspaceLeaseTrust(without_hash["trust"]),  # type: ignore[arg-type]
        authority=WorkspaceLeaseAuthority(without_hash["authority"]),  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(hours=2),
        released_at=None,
        revoked_at=None,
        handoff_refs=tuple(without_hash["handoff_refs"]),  # type: ignore[arg-type]
        artifact_refs=tuple(without_hash["artifact_refs"]),  # type: ignore[arg-type]
        provenance=dict(without_hash["provenance"]),  # type: ignore[arg-type]
        metadata=dict(without_hash["metadata"]),  # type: ignore[arg-type]
        sha256="0" * 64,
    )
    structural = lease.to_mapping(include_integrity=False)
    for key, value in without_hash.items():
        structural[key] = value
    import json
    from hashlib import sha256

    payload["sha256"] = sha256(
        json.dumps(structural, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return payload


def test_from_mapping_rejects_unknown_keys() -> None:
    payload = _lease().to_mapping()
    payload["extra"] = "nope"

    with pytest.raises(WorkspaceLeaseValidationError, match="unknown keys: extra"):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize("field", ["created_at", "updated_at", "expires_at", "released_at", "revoked_at"])
def test_naive_timestamp_strings_are_rejected(field: str) -> None:
    payload = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1)).transition(
        WorkspaceLeaseState.RELEASED,
        at=NOW + timedelta(minutes=2),
    ).to_mapping()
    payload[field] = "2026-08-11T12:00:00"
    payload = _rehash(payload)

    with pytest.raises(WorkspaceLeaseValidationError, match="timezone-aware"):
        WorkspaceLease.from_mapping(payload)


def test_naive_datetime_objects_are_rejected() -> None:
    with pytest.raises(WorkspaceLeaseValidationError, match="created_at must be timezone-aware"):
        requested_workspace_lease(
            lease_id="naive",
            workspace_ref="workspace://durable/naive",
            owner="owner",
            consumer="consumer",
            ttl=timedelta(hours=1),
            created_at=datetime(2026, 8, 11, 12, 0),
        )


def test_canonical_timestamp_hash_normalizes_to_utc() -> None:
    lease = requested_workspace_lease(
        lease_id="tz",
        workspace_ref="workspace://durable/tz",
        owner="owner",
        consumer="consumer",
        ttl=timedelta(hours=1),
        created_at=datetime.fromisoformat("2026-08-11T08:00:00-04:00"),
    )

    assert lease.to_mapping()["created_at"] == "2026-08-11T12:00:00Z"
    assert WorkspaceLease.from_mapping(lease.to_mapping()).sha256 == lease.sha256


def test_updated_at_must_not_precede_release_or_revoke_timestamp() -> None:
    released = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1)).transition(
        WorkspaceLeaseState.RELEASED,
        at=NOW + timedelta(minutes=5),
    ).to_mapping()
    released["updated_at"] = "2026-08-11T12:04:00Z"
    released = _rehash(released)

    with pytest.raises(WorkspaceLeaseValidationError, match="updated_at cannot be before released_at"):
        WorkspaceLease.from_mapping(released)

    revoked = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1)).transition(
        WorkspaceLeaseState.REVOKED,
        at=NOW + timedelta(minutes=5),
    ).to_mapping()
    revoked["updated_at"] = "2026-08-11T12:04:00Z"
    revoked = _rehash(revoked)

    with pytest.raises(WorkspaceLeaseValidationError, match="updated_at cannot be before revoked_at"):
        WorkspaceLease.from_mapping(revoked)


@pytest.mark.parametrize("state", [WorkspaceLeaseState.RELEASED, WorkspaceLeaseState.REVOKED])
def test_release_or_revoke_at_or_after_expiry_is_invalid(state: WorkspaceLeaseState) -> None:
    active = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1))

    with pytest.raises(WorkspaceLeaseValidationError, match="at or after expires_at"):
        active.transition(state, at=NOW + timedelta(hours=2))


@pytest.mark.parametrize("state", [WorkspaceLeaseState.REQUESTED, WorkspaceLeaseState.ACTIVE])
def test_requested_and_active_records_fail_closed_when_structurally_expired(state: WorkspaceLeaseState) -> None:
    payload = _lease().to_mapping()
    payload["state"] = state.value
    payload["updated_at"] = payload["expires_at"]
    payload = _rehash(payload)

    with pytest.raises(WorkspaceLeaseValidationError, match="structurally expired"):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("owner", "@ambiguous", "ambiguous identifier format"),
        ("consumer", "team/tool", "ambiguous identifier format"),
        ("owner", " owner", "leading or trailing whitespace"),
        ("consumer", "con sumer", "must not contain whitespace"),
    ],
)
def test_owner_and_consumer_reject_ambiguous_or_whitespace_identifiers(field: str, value: str, match: str) -> None:
    payload = _lease().to_mapping()
    payload[field] = value
    payload = _rehash(payload)

    with pytest.raises(WorkspaceLeaseValidationError, match=match):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize("field", ["workspace_ref", "handoff_refs", "artifact_refs"])
def test_refs_must_be_opaque_uri_references(field: str) -> None:
    payload = _lease().to_mapping()
    payload[field] = ["blank"] if field.endswith("refs") else "blank"
    payload = _rehash(payload)

    with pytest.raises(WorkspaceLeaseValidationError, match="opaque URI reference"):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize("field", ["handoff_refs", "artifact_refs"])
def test_duplicate_refs_fail_closed(field: str) -> None:
    payload = _lease().to_mapping()
    payload[field] = ["artifact://dup", "artifact://dup"]
    payload = _rehash(payload)

    with pytest.raises(WorkspaceLeaseValidationError, match="duplicate refs"):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize("digest", ["0" * 63, "0" * 65, "A" * 64, "g" * 64])
def test_integrity_hash_must_be_exactly_lowercase_sha256_hex(digest: str) -> None:
    payload = _lease().to_mapping()
    payload["sha256"] = digest

    with pytest.raises(WorkspaceLeaseValidationError, match="64 lowercase hex"):
        WorkspaceLease.from_mapping(payload)


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
def test_integrity_hash_detects_tampering_of_each_structural_field(field: str) -> None:
    payload = _lease().transition(WorkspaceLeaseState.ACTIVE, at=NOW + timedelta(minutes=1)).transition(
        WorkspaceLeaseState.REVOKED,
        at=NOW + timedelta(minutes=2),
    ).to_mapping()
    if field == "created_at":
        payload[field] = "2026-08-11T11:59:30Z"
    elif field == "updated_at":
        payload[field] = "2026-08-11T12:02:30Z"
    elif field in {"released_at", "revoked_at"}:
        payload[field] = "2026-08-11T12:01:30Z"
    elif field == "expires_at":
        payload[field] = "2026-08-11T12:30:00Z"
    elif field in {"handoff_refs", "artifact_refs"}:
        payload[field] = ["artifact://tampered"]
    elif field in {"provenance", "metadata"}:
        payload[field] = {"schema": "workspace-lease-v1", "tampered": "yes"}
    elif field == "state":
        payload[field] = WorkspaceLeaseState.EXPIRED.value
        payload["updated_at"] = payload["expires_at"]
    elif field == "classification":
        payload[field] = WorkspaceLeaseSourceClassification.ARTIFACT.value
    elif field == "trust":
        payload[field] = WorkspaceLeaseTrust.VERIFIED_METADATA.value
        payload["authority"] = WorkspaceLeaseAuthority.EVIDENCE.value
    elif field == "authority":
        payload[field] = WorkspaceLeaseAuthority.EVIDENCE.value
    elif field == "workspace_ref":
        payload[field] = "workspace://durable/tampered"
    elif field in {"owner", "consumer"}:
        payload[field] = "tampered"
    else:
        payload[field] = "tampered"

    with pytest.raises(WorkspaceLeaseValidationError, match="integrity hash is missing or invalid"):
        WorkspaceLease.from_mapping(payload)


def test_transition_rejects_raw_string_state_and_naive_at_cleanly() -> None:
    lease = _lease()

    with pytest.raises(WorkspaceLeaseValidationError, match="transition state"):
        lease.transition("active", at=NOW + timedelta(minutes=1))  # type: ignore[arg-type]

    with pytest.raises(WorkspaceLeaseValidationError, match="at must be timezone-aware"):
        lease.transition(WorkspaceLeaseState.ACTIVE, at=datetime(2026, 8, 11, 12, 1))