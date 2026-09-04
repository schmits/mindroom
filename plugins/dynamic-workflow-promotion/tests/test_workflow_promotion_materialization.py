from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.tool_system import runtime_context

_IMPL_PATH = Path(__file__).resolve().parents[1] / "workflow_promotion_impl.py"
_SPEC = importlib.util.spec_from_file_location("workflow_promotion_impl_materialization_under_test", _IMPL_PATH)
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


def test_apply_materializes_room_scoped_workflow_for_runtime_consumption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_shared_artifacts(tmp_path)
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "ok"
    workflow_dir = tmp_path / "runtime" / "dynamic_workflows" / "room" / "hash_03cbb71e7ec22cfce1875295" / "review_flow"
    workflow_summary = workflow_dir / "workflow.yaml"
    workflow_revision = workflow_dir / "revisions" / "000001.yaml"
    assert workflow_summary.is_file()
    assert workflow_revision.is_file()
    summary = yaml.safe_load(workflow_summary.read_text(encoding="utf-8"))
    assert summary["scope"] == "room"
    assert summary["owner_id"] == "!room:example.org"
    with _runtime_context(tmp_path):
        materialized = DynamicWorkflowStore(tmp_path / "runtime").load_workflow_revision(
            workflow_id="review_flow",
            scope="room",
            owner_id="!room:example.org",
            revision="000001",
        )
    assert materialized == _normalized_workflow_spec()
    assert _sha256_json(materialized) == _sha256_json(_normalized_workflow_spec())


def test_apply_materializes_tenant_scoped_workflow_for_runtime_consumption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_shared_artifacts(tmp_path, target_scope="tenant", target_ref="tenant")
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, target_scope="tenant", target_ref="tenant")

    data = json.loads(payload)
    assert data["status"] == "ok"
    with _runtime_context(tmp_path):
        summary = DynamicWorkflowStore(tmp_path / "runtime").get_workflow(
            workflow_id="review_flow",
            scope="tenant",
            owner_id="tenant",
        )
    assert summary.scope == "tenant"
    assert summary.owner_id == "tenant"


def test_existing_workflow_update_replaces_active_revision_with_exact_approved_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = _write_shared_artifacts(tmp_path)
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    existing_spec = {
        **_normalized_workflow_spec(),
        "description": "must be removed by exact promotion",
        "inputs": {"type": "object", "properties": {"stale_input": {"type": "string"}}},
        "permissions": {"max_total_agents": 16, "tools": [], "data": {}, "models": ["stale-model"]},
    }
    store = DynamicWorkflowStore(tmp_path / "runtime")
    store.create_workflow(
        spec=existing_spec,
        scope="room",
        owner_id="!room:example.org",
        created_by="seed",
        reason="seed stale runtime revision",
    )
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "ok"
    summary = store.get_workflow(workflow_id="review_flow", scope="room", owner_id="!room:example.org")
    assert summary.active_revision == "000002"
    active_spec = store.load_workflow_revision(
        workflow_id="review_flow",
        scope="room",
        owner_id="!room:example.org",
        revision=summary.active_revision,
    )
    expected = _normalized_workflow_spec()
    assert active_spec == expected
    assert "description" not in active_spec
    assert "inputs" not in active_spec
    assert "models" not in active_spec["permissions"]
    assert _sha256_json(active_spec) == _sha256_json(expected)


def test_apply_does_not_materialize_when_audit_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_shared_artifacts(tmp_path)
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    def fail_audit(path: Path, *_args: object, **_kwargs: object) -> None:
        if path.parent.name == "audit":
            raise OSError("simulated audit failure")
        raise AssertionError("promotion write must not run before durable audit succeeds")

    monkeypatch.setattr(_MODULE, "write_json_file_durable", fail_audit)

    with _runtime_context(tmp_path):
        payload = _call(tool, refs)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "simulated audit failure" in data["message"]
    assert not (tmp_path / "runtime" / "dynamic_workflows").exists()


def test_materialization_failure_persists_only_pending_projection_and_retry_can_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = _write_shared_artifacts(tmp_path)
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated materialization failure")

    monkeypatch.setattr(tool, "_materialize_promoted_workflow", fail_materialization)

    with _runtime_context(tmp_path):
        failed_payload = _call(tool, refs)

    failed = json.loads(failed_payload)
    assert failed["status"] == "error"
    assert "simulated materialization failure" in failed["message"]
    assert not (tmp_path / "state" / "promotions").exists()
    pending_records = list((tmp_path / "state" / "pending").glob("*.json"))
    assert len(pending_records) == 1
    pending = json.loads(pending_records[0].read_text(encoding="utf-8"))
    assert pending["status"] == "materialization_pending"
    assert pending["spec_hash"] == refs["spec_hash"]

    retry_tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))
    with _runtime_context(tmp_path):
        retry_payload = _call(retry_tool, refs)

    retry = json.loads(retry_payload)
    assert retry["status"] == "ok"
    assert Path(retry["promotion_record_ref"]).is_file()
    assert json.loads(Path(retry["promotion_record_ref"]).read_text(encoding="utf-8"))["status"] == "active"
    assert not pending_records[0].exists()


def test_thread_scope_is_rejected_before_persistence_or_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = _write_shared_artifacts(tmp_path, target_scope="thread", target_ref="!room:example.org#$event:example.org")
    monkeypatch.setattr(_MODULE, "ensure_tool_registry_loaded", lambda _runtime_paths: None)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, target_scope="thread", target_ref="!room:example.org#$event:example.org")

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Thread-scoped Dynamic Workflow materialization is not supported" in data["message"]
    assert not (tmp_path / "state" / "promotions").exists()
    assert not (tmp_path / "state" / "pending").exists()
    assert not (tmp_path / "runtime" / "dynamic_workflows").exists()


def _call(
    tool: DynamicWorkflowPromotionTools,
    refs: dict[str, str],
    *,
    target_scope: str = "room",
    target_ref: str = "!room:example.org",
) -> str:
    return tool.promote_dynamic_workflow_spec(
        workflow_spec_ref=refs["spec_ref"],
        validated_artifact_ref=refs["validation_ref"],
        target_scope=target_scope,
        target_ref=target_ref,
        approved_by="@approver:example.org",
        approval_evidence_ref=refs["approval_ref"],
        expected_spec_hash=refs["spec_hash"],
        dry_run=False,
        preflight=False,
        rollback_policy={"action": "disable", "expected_spec_hash": refs["spec_hash"]},
        reason="approved narrow promotion",
    )


def _write_shared_artifacts(tmp_path: Path, *, target_scope: str = "room", target_ref: str = "!room:example.org") -> dict[str, str]:
    artifacts_root = tmp_path / "runtime" / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    spec_path = artifacts_root / "workflow.json"
    spec_path.write_text(json.dumps(WORKFLOW_SPEC, sort_keys=True), encoding="utf-8")
    spec_hash = _sha256_file(spec_path)
    validation = {
        "schema_version": 1,
        "status": "validated",
        "workflow_id": WORKFLOW_SPEC["id"],
        "spec_hash": spec_hash,
        "target_scope": target_scope,
        "target_ref": target_ref,
        "approved_by": "@approver:example.org",
    }
    approval = {
        "schema_version": 1,
        "status": "approved",
        "workflow_id": WORKFLOW_SPEC["id"],
        "spec_hash": spec_hash,
        "target_scope": target_scope,
        "target_ref": target_ref,
        "approved_by": "@approver:example.org",
        "event_id": "$approval:example.org",
        "approved_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "reason": "approved narrow promotion",
    }
    (artifacts_root / "validation.json").write_text(json.dumps(validation, sort_keys=True), encoding="utf-8")
    (artifacts_root / "approval.json").write_text(json.dumps(approval, sort_keys=True), encoding="utf-8")
    return {
        "spec_ref": "artifact://workflow.json",
        "validation_ref": "artifact://validation.json",
        "approval_ref": "artifact://approval.json",
        "spec_hash": spec_hash,
    }


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_workflow_spec() -> dict[str, object]:
    return {
        **WORKFLOW_SPEC,
        "participants": [{"id": "writer", "kind": "ephemeral_agent", "name": "Writer", "tools": []}],
        "permissions": {"max_total_agents": 16, "tools": [], "data": {}},
        "outputs": [],
    }


def _sha256_json(data: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class _runtime_context:
    def __init__(self, tmp_path: Path) -> None:
        self._manager = None
        self._context = SimpleNamespace(
            requester_id="@requester:example.org",
            agent_name="Toolsmith",
            runtime_paths=SimpleNamespace(storage_root=tmp_path / "runtime"),
        )

    def __enter__(self) -> None:
        self._manager = runtime_context.tool_runtime_context(self._context)  # type: ignore[arg-type]
        self._manager.__enter__()

    def __exit__(self, *_exc: object) -> None:
        assert self._manager is not None
        self._manager.__exit__(*_exc)
