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