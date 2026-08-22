"""Metadata-only artifact/workspace lease integration tests."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

import importlib.util
import sys
from pathlib import Path

_PLUGIN_MODULE_PATH = Path(__file__).resolve().parents[1] / "artifact_lease_links.py"
_SPEC = importlib.util.spec_from_file_location("repo_workspace_artifact_lease_links", _PLUGIN_MODULE_PATH)
assert _SPEC is not None
artifact_lease_links = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = artifact_lease_links
_SPEC.loader.exec_module(artifact_lease_links)
ArtifactLeaseLink = artifact_lease_links.ArtifactLeaseLink
ArtifactLeaseLinkValidationError = artifact_lease_links.ArtifactLeaseLinkValidationError
LinkedRecordAuthority = artifact_lease_links.LinkedRecordAuthority
from mindroom.handoff_artifacts import HandoffArtifact, HandoffAuthority, HandoffSourceClassification, HandoffTrust
from mindroom.workspace_leases import (
    WorkspaceLease,
    WorkspaceLeaseAuthority,
    WorkspaceLeaseSourceClassification,
    WorkspaceLeaseState,
    WorkspaceLeaseTrust,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _artifact(
    *,
    artifact_id: str = "artifact-1",
    artifact_ref: str = "artifact://handoff-summary.json",
    lease_ref: str = "lease://lease-1",
    classification: HandoffSourceClassification = HandoffSourceClassification.ARTIFACT,
    trust: HandoffTrust = HandoffTrust.VERIFIED_ARTIFACT,
    authority: HandoffAuthority = HandoffAuthority.EVIDENCE,
    materialize_allowed: bool = False,
) -> HandoffArtifact:
    return HandoffArtifact.create(
        artifact_id=artifact_id,
        classification=classification,
        producer="toolsmith",
        consumer="mind",
        trust=trust,
        authority=authority,
        refs=(artifact_ref,),
        manifest={"path": "handoff-summary.json"},
        lease_ref=lease_ref,
        materialize_allowed=materialize_allowed,
    )


def _lease(
    *,
    lease_id: str = "lease-1",
    artifact_ref: str = "artifact://handoff-summary.json",
    classification: WorkspaceLeaseSourceClassification = WorkspaceLeaseSourceClassification.ARTIFACT,
    trust: WorkspaceLeaseTrust = WorkspaceLeaseTrust.VERIFIED_METADATA,
    authority: WorkspaceLeaseAuthority = WorkspaceLeaseAuthority.EVIDENCE,
) -> WorkspaceLease:
    return WorkspaceLease.create(
        lease_id=lease_id,
        state=WorkspaceLeaseState.ACTIVE,
        workspace_ref="workspace://durable/lease-1",
        owner="toolsmith",
        consumer="mind",
        classification=classification,
        trust=trust,
        authority=authority,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        artifact_refs=(artifact_ref,),
        provenance={"schema": "workspace-lease-v1", "source": "artifact"},
    )


def test_artifact_lease_link_round_trips_metadata_only() -> None:
    link = ArtifactLeaseLink.create(artifact=_artifact(), lease=_lease())

    assert link.authority is LinkedRecordAuthority.EVIDENCE
    assert link.artifact_ref == "artifact://handoff-summary.json"
    assert link.lease_ref == "lease://lease-1"
    assert ArtifactLeaseLink.from_mapping(link.to_mapping()) == link


@pytest.mark.parametrize(
    ("artifact_classification", "lease_classification"),
    [
        (HandoffSourceClassification.DESIGN_REFERENCE, WorkspaceLeaseSourceClassification.DESIGN_REFERENCE),
        (HandoffSourceClassification.TARGET_REPO, WorkspaceLeaseSourceClassification.TARGET_REPO),
        (HandoffSourceClassification.RUNTIME_CONFIG, WorkspaceLeaseSourceClassification.RUNTIME_RECORD),
        (HandoffSourceClassification.ARTIFACT, WorkspaceLeaseSourceClassification.ARTIFACT),
        (HandoffSourceClassification.ARTIFACT, WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT),
        (HandoffSourceClassification.UNTRUSTED_REPO_CONTENT, WorkspaceLeaseSourceClassification.UNTRUSTED_REPO_CONTENT),
        (HandoffSourceClassification.IMPLEMENTATION_EVIDENCE, WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT),
        (HandoffSourceClassification.REVIEW_FINDING, WorkspaceLeaseSourceClassification.HANDOFF_ARTIFACT),
    ],
)
def test_link_accepts_explicit_supported_classification_combinations(
    artifact_classification: HandoffSourceClassification,
    lease_classification: WorkspaceLeaseSourceClassification,
) -> None:
    trust = HandoffTrust.UNTRUSTED
    lease_trust = WorkspaceLeaseTrust.UNTRUSTED
    authority = HandoffAuthority.NON_AUTHORITATIVE
    lease_authority = WorkspaceLeaseAuthority.NON_AUTHORITATIVE
    if artifact_classification not in {
        HandoffSourceClassification.DESIGN_REFERENCE,
        HandoffSourceClassification.UNTRUSTED_REPO_CONTENT,
    }:
        trust = HandoffTrust.VERIFIED_ARTIFACT
        lease_trust = WorkspaceLeaseTrust.VERIFIED_METADATA
        authority = HandoffAuthority.EVIDENCE
        lease_authority = WorkspaceLeaseAuthority.EVIDENCE

    link = ArtifactLeaseLink.create(
        artifact=_artifact(
            classification=artifact_classification,
            trust=trust,
            authority=authority,
        ),
        lease=_lease(
            classification=lease_classification,
            trust=lease_trust,
            authority=lease_authority,
        ),
    )

    assert link.lease.classification is lease_classification


@pytest.mark.parametrize(
    "artifact_classification",
    [
        HandoffSourceClassification.RUNTIME_CONFIG,
        HandoffSourceClassification.IMPLEMENTATION_EVIDENCE,
        HandoffSourceClassification.REVIEW_FINDING,
    ],
)
def test_previously_unmapped_artifact_classifications_fail_closed_on_incompatible_lease(
    artifact_classification: HandoffSourceClassification,
) -> None:
    with pytest.raises(ArtifactLeaseLinkValidationError, match="source classifications are inconsistent"):
        ArtifactLeaseLink.create(
            artifact=_artifact(classification=artifact_classification),
            lease=_lease(classification=WorkspaceLeaseSourceClassification.ARTIFACT),
        )


def test_link_rejects_operator_request_lease_classification() -> None:
    with pytest.raises(ArtifactLeaseLinkValidationError, match="source classifications are inconsistent"):
        ArtifactLeaseLink.create(
            artifact=_artifact(classification=HandoffSourceClassification.ARTIFACT),
            lease=_lease(classification=WorkspaceLeaseSourceClassification.OPERATOR_REQUEST),
        )


def test_link_accepts_lease_handoff_refs_for_handoff_artifacts() -> None:
    artifact_ref = "handoff://artifact-1"
    lease = WorkspaceLease.create(
        lease_id="lease-1",
        state=WorkspaceLeaseState.ACTIVE,
        workspace_ref="workspace://durable/lease-1",
        owner="toolsmith",
        consumer="mind",
        classification=WorkspaceLeaseSourceClassification.ARTIFACT,
        trust=WorkspaceLeaseTrust.VERIFIED_METADATA,
        authority=WorkspaceLeaseAuthority.EVIDENCE,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        handoff_refs=(artifact_ref,),
        provenance={"schema": "workspace-lease-v1", "source": "artifact"},
    )

    link = ArtifactLeaseLink.create(
        artifact=_artifact(artifact_ref=artifact_ref),
        lease=lease,
        artifact_ref=artifact_ref,
    )

    assert link.artifact_ref == artifact_ref


def _add_unexpected_key(payload: dict[str, object]) -> None:
    payload["unexpected"] = "nope"


def _remove_lease(payload: dict[str, object]) -> None:
    del payload["lease"]


def _replace_hash(payload: dict[str, object]) -> None:
    payload["sha256"] = "0" * 64


@pytest.mark.parametrize(
    ("payload_mutation", "match"),
    [
        (_add_unexpected_key, "unknown keys: unexpected"),
        (_remove_lease, "missing required keys: lease"),
        (_replace_hash, "integrity hash is missing or invalid"),
    ],
)
def test_link_from_mapping_fails_closed(payload_mutation: Callable[[dict[str, object]], None], match: str) -> None:
    payload = ArtifactLeaseLink.create(artifact=_artifact(), lease=_lease()).to_mapping()
    payload_mutation(payload)

    with pytest.raises(ArtifactLeaseLinkValidationError, match=match):
        ArtifactLeaseLink.from_mapping(payload)


def test_link_rejects_tampered_linked_record_even_if_link_hash_was_original() -> None:
    payload = ArtifactLeaseLink.create(artifact=_artifact(), lease=_lease()).to_mapping()
    payload["artifact"] = dict(payload["artifact"])  # type: ignore[arg-type]
    payload["artifact"]["producer"] = "tampered"  # type: ignore[index]

    with pytest.raises(ArtifactLeaseLinkValidationError, match="invalid linked record"):
        ArtifactLeaseLink.from_mapping(payload)


def test_link_rejects_missing_cross_reference() -> None:
    with pytest.raises(
        ArtifactLeaseLinkValidationError,
        match="workspace lease must reference the linked artifact_ref",
    ):
        ArtifactLeaseLink.create(artifact=_artifact(), lease=_lease(artifact_ref="artifact://other.json"))


def test_link_rejects_custom_artifact_ref_not_in_artifact_refs() -> None:
    with pytest.raises(
        ArtifactLeaseLinkValidationError,
        match="handoff artifact must reference the linked artifact_ref",
    ):
        ArtifactLeaseLink.create(
            artifact=_artifact(),
            lease=_lease(artifact_ref="artifact://custom.json"),
            artifact_ref="artifact://custom.json",
        )


def test_link_rejects_mismatched_lease_ref() -> None:
    with pytest.raises(ArtifactLeaseLinkValidationError, match="lease_ref must match linked lease_ref"):
        ArtifactLeaseLink.create(artifact=_artifact(lease_ref="lease://different"), lease=_lease())


def test_link_rejects_inconsistent_classification_trust_or_authority_labels() -> None:
    with pytest.raises(ArtifactLeaseLinkValidationError, match="source classifications are inconsistent"):
        ArtifactLeaseLink.create(
            artifact=_artifact(classification=HandoffSourceClassification.TARGET_REPO),
            lease=_lease(classification=WorkspaceLeaseSourceClassification.ARTIFACT),
        )

    with pytest.raises(ArtifactLeaseLinkValidationError, match="trust labels are inconsistent"):
        ArtifactLeaseLink.create(artifact=_artifact(trust=HandoffTrust.UNTRUSTED), lease=_lease())

    with pytest.raises(ArtifactLeaseLinkValidationError, match="authority labels are inconsistent"):
        ArtifactLeaseLink.create(artifact=_artifact(authority=HandoffAuthority.NON_AUTHORITATIVE), lease=_lease())


def test_design_reference_link_is_non_authoritative_and_non_materializable() -> None:
    artifact = _artifact(
        artifact_ref="artifact://design-note",
        classification=HandoffSourceClassification.DESIGN_REFERENCE,
        trust=HandoffTrust.UNTRUSTED,
        authority=HandoffAuthority.NON_AUTHORITATIVE,
    )
    lease = _lease(
        artifact_ref="artifact://design-note",
        classification=WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
        trust=WorkspaceLeaseTrust.UNTRUSTED,
        authority=WorkspaceLeaseAuthority.NON_AUTHORITATIVE,
    )

    link = ArtifactLeaseLink.create(artifact=artifact, lease=lease)

    assert link.authority is LinkedRecordAuthority.NON_AUTHORITATIVE
    assert link.artifact.materialize_allowed is False


def test_link_from_mapping_rejects_authority_escalation_for_design_reference() -> None:
    link = ArtifactLeaseLink.create(
        artifact=_artifact(
            artifact_ref="artifact://design-note",
            classification=HandoffSourceClassification.DESIGN_REFERENCE,
            trust=HandoffTrust.UNTRUSTED,
            authority=HandoffAuthority.NON_AUTHORITATIVE,
        ),
        lease=_lease(
            artifact_ref="artifact://design-note",
            classification=WorkspaceLeaseSourceClassification.DESIGN_REFERENCE,
            trust=WorkspaceLeaseTrust.UNTRUSTED,
            authority=WorkspaceLeaseAuthority.NON_AUTHORITATIVE,
        ),
    )
    payload = link.to_mapping()
    payload["authority"] = "evidence"
    # Re-hashing proves label consistency, not only the top-level hash, fails closed.
    forged = ArtifactLeaseLink(
        artifact=link.artifact,
        lease=link.lease,
        artifact_ref=link.artifact_ref,
        lease_ref=link.lease_ref,
        authority=LinkedRecordAuthority.EVIDENCE,
        sha256="pending",
    )
    payload["sha256"] = forged.integrity_hash()

    with pytest.raises(ArtifactLeaseLinkValidationError, match="authority does not match linked records"):
        ArtifactLeaseLink.from_mapping(payload)