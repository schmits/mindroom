from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from mindroom.tool_system import runtime_context


_IMPL_PATH = Path(__file__).resolve().parents[1] / 'workflow_promotion_impl.py'
_SPEC = importlib.util.spec_from_file_location('workflow_promotion_impl_under_test', _IMPL_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
DynamicWorkflowPromotionTools = _MODULE.DynamicWorkflowPromotionTools


WORKFLOW_SPEC = {
    "schema_version": 1,
    "kind": "workflow",
    "id": "review_flow",
    "name": "Review Flow",
    "participants": [{"id": "writer", "kind": "ephemeral_agent", "name": "Writer"}],
    "workflow": [{"id": "draft", "type": "agent_step", "participant": "writer", "prompt": "Draft."}],
}


def test_toolsmith_workspace_relative_workflow_spec_ref_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_artifacts(tmp_path)
    workspace = tmp_path / "toolsmith_workspace"
    workspace.mkdir()
    shutil.copyfile(Path(refs["spec_ref"]), workspace / "workflow.json")
    refs["spec_ref"] = "workflow.json"
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with monkeypatch.context() as patched:
        patched.chdir(workspace)
        with _runtime_context(tmp_path):
            payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "artifact://" in data["message"]
    assert not (tmp_path / "state" / "promotions").exists()


def test_preflight_accepts_canonical_shared_artifact_ref(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, use_shared_refs=True)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "ok"
    assert data["mode"] == "preflight"
    assert data["persisted"] is False


def test_markdown_workflow_spec_wrapper_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, use_shared_refs=True)
    wrapper = tmp_path / "runtime" / "artifacts" / "workflow.md"
    wrapper.write_text(f"See canonical spec at {refs['spec_ref']}\n", encoding="utf-8")
    refs["spec_ref"] = "artifact://workflow.md"
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "not Markdown" in data["message"]


def test_shared_artifact_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, use_shared_refs=True)
    spec_path = tmp_path / "runtime" / "artifacts" / "workflow.json"
    spec_path.write_text(json.dumps({**WORKFLOW_SPEC, "name": "Tampered"}, sort_keys=True), encoding="utf-8")
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Spec substitution" in data["message"]


def test_validation_and_approval_binding_preserved_for_shared_refs(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, use_shared_refs=True)
    validation_path = tmp_path / "runtime" / "artifacts" / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["target_ref"] = "!other:example.org"
    validation_path.write_text(json.dumps(validation, sort_keys=True), encoding="utf-8")
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "validation artifact target_ref mismatch" in data["message"]


def test_tenant_preflight_dry_run_succeeds_only_with_real_shared_artifacts(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, use_shared_refs=True, target_scope="tenant", target_ref="tenant")
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        ok = json.loads(_call(tool, refs, target_scope="tenant", target_ref="tenant", dry_run=True, preflight=True))

    assert ok["status"] == "ok"
    assert ok["mode"] == "dry_run"
    assert ok["persisted"] is False
    assert not (tmp_path / "state" / "promotions").exists()

    refs["spec_ref"] = "artifact://missing.json"
    with _runtime_context(tmp_path):
        missing = json.loads(_call(tool, refs, target_scope="tenant", target_ref="tenant", dry_run=True, preflight=True))

    assert missing["status"] == "error"
    assert "readable file" in missing["message"]


def test_preflight_does_not_persist(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "ok"
    assert data["persisted"] is False
    assert not (tmp_path / "state" / "promotions").exists()
    assert not (tmp_path / "state" / "audit").exists()


def test_apply_persists_promotion_and_audit(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    state = tmp_path / "state"
    tool = DynamicWorkflowPromotionTools(state_root=str(state), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "ok"
    assert data["persisted"] is True
    promotion_path = Path(data["promotion_record_ref"])
    audit_path = Path(data["audit_record_ref"])
    assert promotion_path.is_file()
    assert audit_path.is_file()
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert promotion["workflow_id"] == "review_flow"
    assert promotion["spec_hash"] == refs["spec_hash"]
    assert audit["promotion_id"] == promotion["promotion_id"]
    assert audit["record_hash"] == _sha256_json(promotion)
    assert "previous_promotion_hash" in promotion
    assert "record" not in audit


def test_audit_write_failure_does_not_publish_active_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_artifacts(tmp_path)
    state = tmp_path / "state"
    tool = DynamicWorkflowPromotionTools(state_root=str(state), allowed_artifact_roots=[str(tmp_path)])

    def fail_audit(path: Path, *_args: object, **_kwargs: object) -> None:
        if path.parent.name == "audit":
            raise OSError("simulated audit fsync failure")
        raise AssertionError("promotion write must not run before durable audit succeeds")

    monkeypatch.setattr(_MODULE, "write_json_file_durable", fail_audit)

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "simulated audit fsync failure" in data["message"]
    assert not (state / "promotions").exists()


def test_audit_directory_fsync_failure_does_not_publish_active_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_artifacts(tmp_path)
    state = tmp_path / "state"
    tool = DynamicWorkflowPromotionTools(state_root=str(state), allowed_artifact_roots=[str(tmp_path)])

    def fail_audit_dir(path: Path) -> None:
        if path.name == "audit":
            raise OSError("simulated audit directory fsync failure")

    monkeypatch.setattr(_MODULE, "fsync_directory_durable", fail_audit_dir)

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "simulated audit directory fsync failure" in data["message"]
    assert not (state / "promotions").exists()


def test_expected_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    refs["spec_hash"] = "0" * 64
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Spec substitution" in data["message"]
    assert not (tmp_path / "state" / "promotions").exists()


def test_replay_same_promotion_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        first = json.loads(_call(tool, refs))
        second = json.loads(_call(tool, refs))

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "Approval replay" in second["message"]


def test_forbidden_workflow_tool_fails_closed(tmp_path: Path) -> None:
    spec = dict(WORKFLOW_SPEC)
    spec["permissions"] = {"tools": ["github"]}
    refs = _write_artifacts(tmp_path, spec=spec)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Forbidden Dynamic Workflow tool grant" in data["message"]


def test_expired_revoked_and_redacted_approvals_fail_closed(tmp_path: Path) -> None:
    cases = [
        {"approved_at": (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")},
        {"status": "revoked"},
        {"status": "approved", "redacted": True},
    ]
    for idx, approval_overrides in enumerate(cases):
        case_dir = tmp_path / f"case-{idx}"
        case_dir.mkdir()
        refs = _write_artifacts(case_dir, approval_overrides=approval_overrides)
        tool = DynamicWorkflowPromotionTools(state_root=str(case_dir / "state"), allowed_artifact_roots=[str(case_dir)])

        with _runtime_context(case_dir):
            payload = _call(tool, refs)

        data = json.loads(payload)
        assert data["status"] == "error"
        assert "expired" in data["message"] or "revoked" in data["message"] or "redacted" in data["message"]
        assert not (case_dir / "state" / "promotions").exists()


def test_rollback_authorization_requires_fresh_bound_non_revoked_evidence(tmp_path: Path) -> None:
    state = tmp_path / "state"
    refs = _write_artifacts(tmp_path)
    tool = DynamicWorkflowPromotionTools(state_root=str(state), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        first = json.loads(_call(tool, refs))
    assert first["status"] == "ok"
    previous_hash = _sha256_json(json.loads(Path(first["promotion_record_ref"]).read_text(encoding="utf-8")))

    next_spec = dict(WORKFLOW_SPEC, name="Review Flow v2")
    next_refs = _write_artifacts(tmp_path / "next", spec=next_spec)
    auth_ref = _write_rollback_authorization(tmp_path / "next", previous_hash=previous_hash)
    rollback_policy = {
        "action": "restore_previous",
        "expected_spec_hash": next_refs["spec_hash"],
        "authorization_ref": auth_ref,
        "expected_previous_promotion_hash": previous_hash,
    }
    with _runtime_context(tmp_path):
        second = json.loads(_call(tool, next_refs, rollback_policy=rollback_policy))
    assert second["status"] == "ok"

    revoked_refs = _write_artifacts(tmp_path / "revoked", spec=dict(WORKFLOW_SPEC, name="Review Flow v3"))
    revoked_auth = _write_rollback_authorization(
        tmp_path / "revoked",
        previous_hash=_sha256_json(json.loads(Path(second["promotion_record_ref"]).read_text(encoding="utf-8"))),
        overrides={"status": "revoked"},
    )
    revoked_policy = {
        "action": "delete",
        "expected_spec_hash": revoked_refs["spec_hash"],
        "authorization_ref": revoked_auth,
        "expected_previous_promotion_hash": _sha256_json(json.loads(Path(second["promotion_record_ref"]).read_text(encoding="utf-8"))),
    }
    with _runtime_context(tmp_path):
        revoked = json.loads(_call(tool, revoked_refs, rollback_policy=revoked_policy))
    assert revoked["status"] == "error"
    assert "Rollback authorization" in revoked["message"]


def test_dry_run_and_preflight_validate_without_persisting(tmp_path: Path) -> None:
    bad_refs = _write_artifacts(tmp_path / "bad", approval_overrides={"target_ref": "!other:example.org"})
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        dry_run_bad = json.loads(_call(tool, bad_refs, dry_run=True))
    assert dry_run_bad["status"] == "error"
    assert "target_ref mismatch" in dry_run_bad["message"]
    assert not (tmp_path / "state" / "promotions").exists()

    refs = _write_artifacts(tmp_path / "good")
    with _runtime_context(tmp_path):
        dry_run_ok = json.loads(_call(tool, refs, dry_run=True))
        preflight_ok = json.loads(_call(tool, refs, preflight=True))
    assert dry_run_ok["status"] == "ok"
    assert dry_run_ok["mode"] == "dry_run"
    assert preflight_ok["status"] == "ok"
    assert preflight_ok["mode"] == "preflight"
    assert not (tmp_path / "state" / "promotions").exists()
    assert not (tmp_path / "state" / "audit").exists()


def test_delegated_human_approval_matching_requester_passes_for_agent_builder(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, approved_by="@requester:example.org")
    state = tmp_path / "state"
    tool = DynamicWorkflowPromotionTools(state_root=str(state), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path, agent_name="Agent Builder"):
        payload = _call(tool, refs, approved_by="@requester:example.org")

    data = json.loads(payload)
    assert data["status"] == "ok"
    promotion = json.loads(Path(data["promotion_record_ref"]).read_text(encoding="utf-8"))
    assert promotion["approved_by"] == "@requester:example.org"
    assert promotion["requester_id"] == "@requester:example.org"
    assert promotion["created_by"] == "Agent Builder"


def test_acting_agent_self_approval_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, approved_by="@agent-builder:example.org")
    tool = DynamicWorkflowPromotionTools(
        state_root=str(tmp_path / "state"),
        allowed_artifact_roots=[str(tmp_path)],
        self_actor_ids=["@agent-builder:example.org"],
    )

    with _runtime_context(tmp_path, agent_name="Agent Builder", agent_id="@agent-builder:example.org"):
        payload = _call(tool, refs, approved_by="@agent-builder:example.org")

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Self-promotion" in data["message"]


def test_audit_records_requester_approver_and_promoter(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, approved_by="@requester:example.org")
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path, agent_name="Agent Builder", agent_id="@agent-builder:example.org"):
        payload = _call(tool, refs, approved_by="@requester:example.org")

    data = json.loads(payload)
    assert data["status"] == "ok"
    promotion = json.loads(Path(data["promotion_record_ref"]).read_text(encoding="utf-8"))
    audit = json.loads(Path(data["audit_record_ref"]).read_text(encoding="utf-8"))
    assert promotion["approved_by"] == "@requester:example.org"
    assert promotion["requester_id"] == "@requester:example.org"
    assert promotion["created_by"] == "Agent Builder"
    assert audit["approved_by"] == "@requester:example.org"
    assert audit["requester_id"] == "@requester:example.org"
    assert audit["created_by"] == "Agent Builder"


def _call(
    tool: DynamicWorkflowPromotionTools,
    refs: dict[str, str],
    *,
    approved_by: str = "@approver:example.org",
    preflight: bool = False,
    dry_run: bool = False,
    rollback_policy: dict[str, object] | None = None,
    target_scope: str = "room",
    target_ref: str = "!room:example.org",
) -> str:
    return tool.promote_dynamic_workflow_spec(
        workflow_spec_ref=refs["spec_ref"],
        validated_artifact_ref=refs["validation_ref"],
        target_scope=target_scope,
        target_ref=target_ref,
        approved_by=approved_by,
        approval_evidence_ref=refs["approval_ref"],
        expected_spec_hash=refs["spec_hash"],
        dry_run=dry_run,
        preflight=preflight,
        rollback_policy=rollback_policy or {"action": "disable", "expected_spec_hash": refs["spec_hash"]},
        reason="approved narrow promotion",
    )


def _write_artifacts(
    tmp_path: Path,
    *,
    spec: dict[str, object] | None = None,
    approved_by: str = "@approver:example.org",
    approval_overrides: dict[str, object] | None = None,
    use_shared_refs: bool = False,
    target_scope: str = "room",
    target_ref: str = "!room:example.org",
) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workflow_spec = spec or WORKFLOW_SPEC
    spec_path = tmp_path / "workflow.json"
    _write_json(spec_path, workflow_spec)
    spec_hash = _sha256_file(spec_path)
    validation = {
        "schema_version": 1,
        "status": "validated",
        "workflow_id": workflow_spec["id"],
        "spec_hash": spec_hash,
        "target_scope": target_scope,
        "target_ref": target_ref,
        "approved_by": approved_by,
    }
    approval = {
        "schema_version": 1,
        "status": "approved",
        "workflow_id": workflow_spec["id"],
        "spec_hash": spec_hash,
        "target_scope": target_scope,
        "target_ref": target_ref,
        "approved_by": approved_by,
        "event_id": "$approval:example.org",
        "approved_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "reason": "approved narrow promotion",
    }
    if approval_overrides:
        approval.update(approval_overrides)
    validation_path = tmp_path / "validation.json"
    approval_path = tmp_path / "approval.json"
    _write_json(validation_path, validation)
    _write_json(approval_path, approval)
    if use_shared_refs:
        artifacts_root = tmp_path / "runtime" / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        shared_spec = artifacts_root / "workflow.json"
        shared_validation = artifacts_root / "validation.json"
        shared_approval = artifacts_root / "approval.json"
        shutil.copyfile(spec_path, shared_spec)
        shutil.copyfile(validation_path, shared_validation)
        shutil.copyfile(approval_path, shared_approval)
        return {
            "spec_ref": "artifact://workflow.json",
            "validation_ref": "artifact://validation.json",
            "approval_ref": "artifact://approval.json",
            "spec_hash": spec_hash,
        }
    return {
        "spec_ref": str(spec_path),
        "validation_ref": str(validation_path),
        "approval_ref": str(approval_path),
        "spec_hash": spec_hash,
    }


def _write_rollback_authorization(
    directory: Path,
    *,
    previous_hash: str,
    overrides: dict[str, object] | None = None,
) -> str:
    authorization = {
        "schema_version": 1,
        "status": "approved",
        "workflow_id": "review_flow",
        "target_scope": "room",
        "target_ref": "!room:example.org",
        "approved_by": "@approver:example.org",
        "event_id": "$rollback:example.org",
        "approved_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "rollback_actions": ["restore_previous", "tombstone", "delete"],
        "previous_promotion_hash": previous_hash,
    }
    if overrides:
        authorization.update(overrides)
    path = directory / "rollback-authorization.json"
    _write_json(path, authorization)
    return str(path)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _sha256_json(data: object) -> str:
    import hashlib

    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class _runtime_context:
    def __init__(
        self,
        tmp_path: Path,
        *,
        requester_id: str = "@requester:example.org",
        agent_name: str = "Toolsmith",
        agent_id: str | None = None,
    ) -> None:
        self._manager = None
        self._context = SimpleNamespace(
            requester_id=requester_id,
            agent_name=agent_name,
            agent_id=agent_id,
            runtime_paths=SimpleNamespace(storage_root=tmp_path / "runtime"),
        )

    def __enter__(self) -> None:
        self._manager = runtime_context.tool_runtime_context(self._context)  # type: ignore[arg-type]
        self._manager.__enter__()

    def __exit__(self, *_exc: object) -> None:
        assert self._manager is not None
        self._manager.__exit__(*_exc)