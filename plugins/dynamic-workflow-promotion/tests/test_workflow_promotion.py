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


def test_absolute_local_artifact_refs_require_allowed_roots(tmp_path: Path) -> None:
    refs = _write_artifacts(tmp_path)
    tool = DynamicWorkflowPromotionTools(state_root=str(tmp_path / "state"))

    with _runtime_context(tmp_path):
        payload = _call(tool, refs, preflight=True)

    data = json.loads(payload)
    assert data["status"] == "error"
    assert "allowed_artifact_roots" in data["message"]
    assert not (tmp_path / "state" / "promotions").exists()