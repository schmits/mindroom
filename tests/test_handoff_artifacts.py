"""Fail-closed durable handoff artifact metadata tests."""

from __future__ import annotations

import pytest

from mindroom.handoff_artifacts import (
    HandoffArtifact,
    HandoffArtifactValidationError,
    HandoffAuthority,
    HandoffSourceClassification,
    HandoffTrust,
    repo_authored_instruction_artifact,
)


def test_design_reference_is_non_authoritative_and_not_materialized() -> None:
    artifact = HandoffArtifact.create(
        artifact_id="design-ref-1",
        classification=HandoffSourceClassification.DESIGN_REFERENCE,
        producer="@mindroom:example.test",
        consumer="@toolsmith:example.test",
        trust=HandoffTrust.UNTRUSTED,
        authority=HandoffAuthority.NON_AUTHORITATIVE,
        refs=("https://example.invalid/design.md",),
        manifest={"kind": "design-note"},
    )

    assert artifact.classification is HandoffSourceClassification.DESIGN_REFERENCE
    assert artifact.authority is HandoffAuthority.NON_AUTHORITATIVE
    assert artifact.trust is HandoffTrust.UNTRUSTED
    assert artifact.materialize_allowed is False


def test_design_reference_cannot_be_authoritative() -> None:
    with pytest.raises(HandoffArtifactValidationError, match="design_reference handoffs must be non-authoritative"):
        HandoffArtifact.create(
            artifact_id="design-ref-2",
            classification=HandoffSourceClassification.DESIGN_REFERENCE,
            producer="producer",
            consumer="consumer",
            trust=HandoffTrust.UNTRUSTED,
            authority=HandoffAuthority.AUTHORITATIVE,
            refs=("https://example.invalid/design.md",),
            manifest={"kind": "design-note"},
        )


def test_design_reference_cannot_request_materialization() -> None:
    with pytest.raises(HandoffArtifactValidationError, match="must not allow materialization"):
        HandoffArtifact.create(
            artifact_id="design-ref-3",
            classification=HandoffSourceClassification.DESIGN_REFERENCE,
            producer="producer",
            consumer="consumer",
            trust=HandoffTrust.UNTRUSTED,
            authority=HandoffAuthority.NON_AUTHORITATIVE,
            refs=("https://example.invalid/design.md",),
            manifest={"kind": "design-note"},
            materialize_allowed=True,
        )


def test_repo_authored_instructions_are_untrusted_repo_content() -> None:
    artifact = repo_authored_instruction_artifact(
        artifact_id="repo-instructions-1",
        producer="target-repo",
        consumer="runtime",
        refs=("repo://owner/name/AGENTS.md",),
        manifest={"path": "AGENTS.md", "commit": "abc123"},
    )

    assert artifact.classification is HandoffSourceClassification.UNTRUSTED_REPO_CONTENT
    assert artifact.trust is HandoffTrust.UNTRUSTED
    assert artifact.authority is HandoffAuthority.NON_AUTHORITATIVE
    assert artifact.materialize_allowed is False
    assert artifact.metadata["repo_authored_instructions"] == "untrusted"


def test_required_metadata_is_validated() -> None:
    payload = HandoffArtifact.create(
        artifact_id="missing-classification",
        classification=HandoffSourceClassification.UNTRUSTED_REPO_CONTENT,
        producer="producer",
        consumer="consumer",
        trust=HandoffTrust.UNTRUSTED,
        authority=HandoffAuthority.NON_AUTHORITATIVE,
        refs=("repo://owner/name/path",),
        manifest={"path": "path"},
    ).to_mapping()
    payload["classification"] = None
    with pytest.raises(HandoffArtifactValidationError, match="classification is required"):
        HandoffArtifact.from_mapping(payload)

    with pytest.raises(HandoffArtifactValidationError, match="refs must include at least one reference"):
        HandoffArtifact.create(
            artifact_id="missing-refs",
            classification=HandoffSourceClassification.ARTIFACT,
            producer="producer",
            consumer="consumer",
            trust=HandoffTrust.VERIFIED_ARTIFACT,
            authority=HandoffAuthority.EVIDENCE,
            refs=(),
            manifest={"path": "artifact.json"},
        )


def test_invalid_authority_fails_closed() -> None:
    with pytest.raises(HandoffArtifactValidationError, match="untrusted handoff artifacts cannot be authoritative"):
        HandoffArtifact.create(
            artifact_id="bad-authority",
            classification=HandoffSourceClassification.ARTIFACT,
            producer="producer",
            consumer="consumer",
            trust=HandoffTrust.UNTRUSTED,
            authority=HandoffAuthority.AUTHORITATIVE,
            refs=("artifact://report.json",),
            manifest={"path": "report.json"},
        )


def test_integrity_hash_is_required_and_checked() -> None:
    artifact = HandoffArtifact.create(
        artifact_id="evidence-1",
        classification=HandoffSourceClassification.IMPLEMENTATION_EVIDENCE,
        producer="toolsmith",
        consumer="reviewer",
        trust=HandoffTrust.VERIFIED_ARTIFACT,
        authority=HandoffAuthority.EVIDENCE,
        refs=("artifact://diff.patch",),
        manifest={"path": "diff.patch"},
    )
    payload = artifact.to_mapping()
    payload["producer"] = "tampered"

    with pytest.raises(HandoffArtifactValidationError, match="integrity hash is missing or invalid"):
        HandoffArtifact.from_mapping(payload)


def test_from_mapping_rejects_invalid_lease_ref_types() -> None:
    artifact = HandoffArtifact.create(
        artifact_id="lease-ref-type",
        classification=HandoffSourceClassification.ARTIFACT,
        producer="producer",
        consumer="consumer",
        trust=HandoffTrust.VERIFIED_ARTIFACT,
        authority=HandoffAuthority.EVIDENCE,
        refs=("artifact://report.json",),
        manifest={"path": "report.json"},
        lease_ref="lease://valid",
    )

    for invalid_lease_ref in (False, 1, [], {}, object()):
        payload = artifact.to_mapping()
        payload["lease_ref"] = invalid_lease_ref
        with pytest.raises(HandoffArtifactValidationError, match="lease_ref must be a non-empty string"):
            HandoffArtifact.from_mapping(payload)


def test_from_mapping_rejects_non_boolean_materialize_allowed() -> None:
    artifact = HandoffArtifact.create(
        artifact_id="materialize-type",
        classification=HandoffSourceClassification.ARTIFACT,
        producer="producer",
        consumer="consumer",
        trust=HandoffTrust.VERIFIED_ARTIFACT,
        authority=HandoffAuthority.EVIDENCE,
        refs=("artifact://report.json",),
        manifest={"path": "report.json"},
    )

    for invalid_materialize_allowed in ("false", "no", 1, [], None):
        payload = artifact.to_mapping()
        payload["materialize_allowed"] = invalid_materialize_allowed
        with pytest.raises(HandoffArtifactValidationError, match="materialize_allowed must be a boolean"):
            HandoffArtifact.from_mapping(payload)


def test_from_mapping_rejects_unknown_keys() -> None:
    payload = HandoffArtifact.create(
        artifact_id="unknown-key",
        classification=HandoffSourceClassification.ARTIFACT,
        producer="producer",
        consumer="consumer",
        trust=HandoffTrust.VERIFIED_ARTIFACT,
        authority=HandoffAuthority.EVIDENCE,
        refs=("artifact://report.json",),
        manifest={"path": "report.json"},
    ).to_mapping()
    payload["extra"] = "nope"

    with pytest.raises(HandoffArtifactValidationError, match="unknown keys: extra"):
        HandoffArtifact.from_mapping(payload)


def test_from_mapping_requires_full_schema_even_for_defaults() -> None:
    payload = HandoffArtifact.create(
        artifact_id="missing-defaulted",
        classification=HandoffSourceClassification.ARTIFACT,
        producer="producer",
        consumer="consumer",
        trust=HandoffTrust.VERIFIED_ARTIFACT,
        authority=HandoffAuthority.EVIDENCE,
        refs=("artifact://report.json",),
        manifest={"path": "report.json"},
    ).to_mapping()
    del payload["metadata"]

    with pytest.raises(HandoffArtifactValidationError, match="missing required keys: metadata"):
        HandoffArtifact.from_mapping(payload)