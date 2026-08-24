from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    assert "record" not in audit


def test_expected_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    refs["spec_hash"] = "0" * 64
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
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


def test_self_promotion_fails_closed(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path, approved_by="@requester:example.org")
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"), allowed_artifact_roots=[str(tmp_path)])

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, approved_by="@requester:example.org")

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "Self-promotion" in data["message"]


def _call(
    tool: DynamicWorkflowPromotionTools,
    refs: dict[str, str],
    *,
    approved_by: str = "@approver:example.org",
    preflight: bool = False,
) -> str:
    return tool.promote_dynamic_workflow_spec(
        workflow_spec_ref=refs["spec_ref"],
        validated_artifact_ref=refs["validation_ref"],
        target_scope="room",
        target_ref="!room:example.org",
        approved_by=approved_by,
        approval_evidence_ref=refs["approval_ref"],
        expected_spec_hash=refs["spec_hash"],
        dry_run=False,
        preflight=preflight,
        rollback_policy={"action": "disable", "expected_spec_hash": refs["spec_hash"]},
        reason="approved narrow promotion",
    )


def _write_artifacts(
    tmp_path: Path,
    *,
    spec: dict[str, object] | None = None,
    approved_by: str = "@approver:example.org",
) -> dict[str, str]:
    workflow_spec = spec or WORKFLOW_SPEC
    spec_path = tmp_path / "workflow.json"
    _write_json(spec_path, workflow_spec)
    spec_hash = _sha256_file(spec_path)
    validation = {
        "schema_version": 1,
        "status": "validated",
        "workflow_id": workflow_spec["id"],
        "spec_hash": spec_hash,
        "target_scope": "room",
        "target_ref": "!room:example.org",
        "approved_by": approved_by,
    }
    approval = {
        "schema_version": 1,
        "status": "approved",
        "workflow_id": workflow_spec["id"],
        "spec_hash": spec_hash,
        "target_scope": "room",
        "target_ref": "!room:example.org",
        "approved_by": approved_by,
        "event_id": "$approval:example.org",
        "approved_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "reason": "approved narrow promotion",
    }
    validation_path = tmp_path / "validation.json"
    approval_path = tmp_path / "approval.json"
    _write_json(validation_path, validation)
    _write_json(approval_path, approval)
    return {
        "spec_ref": str(spec_path),
        "validation_ref": str(validation_path),
        "approval_ref": str(approval_path),
        "spec_hash": spec_hash,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


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