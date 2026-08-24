"""Shared Dynamic Workflow helpers for runtime-context-aware tools."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, cast

from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowStore
from mindroom.dynamic_workflows.validation import DynamicWorkflowError
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.runtime_context import ToolRuntimeContext, build_execution_identity_from_runtime_context

_PROMOTION_PLUGIN_NAME = "dynamic-workflow-promotion"
_PROMOTION_SCHEMA_VERSION = 1
_PROMOTABLE_SCOPES = frozenset({"room", "tenant"})


def dynamic_workflow_store(context: ToolRuntimeContext) -> DynamicWorkflowStore:
    """Return the Dynamic Workflow store for the current tool runtime."""
    return DynamicWorkflowStore(context.runtime_paths.storage_root)


def dynamic_workflow_store_and_owner(
    context: ToolRuntimeContext,
    scope: str,
) -> tuple[DynamicWorkflowStore, str]:
    """Return the Dynamic Workflow store and caller-visible owner ID.

    Room/tenant mutation paths remain write-protected. Read/run paths consume
    already-audited promotion records by projecting them into the ordinary Dynamic
    Workflow store before lookup.
    """
    if not context.agent_name:
        msg = "Agent name is missing in the tool runtime context."
        raise DynamicWorkflowError(msg)
    if scope in _PROMOTABLE_SCOPES:
        if _shared_scope_mutation_call():
            msg = f"{scope} scope requires Dynamic Workflow approval policy and is not available to agent tools yet."
            raise DynamicWorkflowError(msg)
        store = dynamic_workflow_store(context)
        owner_id = _dynamic_workflow_owner_id(context, scope)
        _materialize_active_promotions(context, store, scope=scope, owner_id=owner_id)
        return store, owner_id
    return dynamic_workflow_store(context), _dynamic_workflow_owner_id(context, scope)


def _dynamic_workflow_owner_id(context: ToolRuntimeContext, scope: str) -> str:
    """Resolve the owner ID for a Dynamic Workflow scope."""
    if scope == "agent":
        return _agent_scope_owner_id(context)
    if scope == "room":
        if not context.room_id:
            msg = "Room ID is missing in the tool runtime context."
            raise DynamicWorkflowError(msg)
        return context.room_id
    if scope == "tenant":
        return "tenant"
    msg = f"Unsupported Dynamic Workflow scope '{scope}'."
    raise DynamicWorkflowError(msg)


def authorize_dynamic_workflow_run(context: ToolRuntimeContext, run: DynamicWorkflowRun) -> None:
    """Require the current requester to match the run requester."""
    if run.requested_by != context.requester_id:
        msg = "Dynamic Workflow run is not available to the current requester."
        raise DynamicWorkflowError(msg)


def _shared_scope_mutation_call() -> bool:
    """Return true when the Dynamic Workflow tool is directly mutating shared scope."""
    for frame in inspect.stack(context=0)[1:8]:
        if frame.function in {"create_workflow", "update_workflow", "acreate_workflow", "aupdate_workflow"}:
            return True
    return False


def _materialize_active_promotions(
    context: ToolRuntimeContext,
    store: DynamicWorkflowStore,
    *,
    scope: str,
    owner_id: str,
) -> None:
    """Materialize audited promotion records into the Dynamic Workflow store.

    Promotion remains fail-closed: malformed or hash-mismatched promotion records are
    ignored, and only records explicitly active for the current owner are consumed.
    The promotion plugin is still the sole writer for approvals/audit; this bridge
    only projects valid active records into the runtime store that lookup/run use.
    """
    promotions_dir = context.runtime_paths.storage_root / "plugins" / _PROMOTION_PLUGIN_NAME / "promotions"
    if not promotions_dir.is_dir():
        return
    for promotion_path in sorted(promotions_dir.glob("*.json")):
        promotion = _load_promotion_record(promotion_path)
        if promotion is None or not _promotion_targets_owner(promotion, scope=scope, owner_id=owner_id):
            continue
        _materialize_promotion(store, promotion)


def _load_promotion_record(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast("dict[str, object]", data)


def _promotion_targets_owner(promotion: dict[str, object], *, scope: str, owner_id: str) -> bool:
    if promotion.get("schema_version") != _PROMOTION_SCHEMA_VERSION or promotion.get("status") != "active":
        return False
    if promotion.get("target_scope") != scope or promotion.get("target_ref") != owner_id:
        return False
    return isinstance(promotion.get("workflow_id"), str) and isinstance(promotion.get("workflow_spec_ref"), str)


def _materialize_promotion(store: DynamicWorkflowStore, promotion: dict[str, object]) -> None:
    spec_ref = cast("str", promotion["workflow_spec_ref"])
    expected_hash = cast("str | None", promotion.get("spec_hash"))
    spec = _read_hash_bound_spec(spec_ref, expected_hash)
    workflow_id = str(promotion["workflow_id"])
    if spec.get("id") != workflow_id:
        raise DynamicWorkflowError("Promotion record workflow_id does not match workflow spec.")
    reason = str(promotion.get("reason") or "audited Dynamic Workflow promotion")
    actor = str(promotion.get("created_by") or "dynamic_workflow_promotion")
    scope = str(promotion["target_scope"])
    owner_id = str(promotion["target_ref"])
    try:
        store.get_workflow(workflow_id=workflow_id, scope=scope, owner_id=owner_id)
    except DynamicWorkflowError:
        store.create_workflow(spec=spec, scope=scope, owner_id=owner_id, created_by=actor, reason=reason)


def _read_hash_bound_spec(spec_ref: str, expected_hash: str | None) -> dict[str, object]:
    if not expected_hash:
        raise DynamicWorkflowError("Promotion record is missing spec_hash.")
    path = Path(spec_ref).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        raw = path.resolve().read_bytes()
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DynamicWorkflowError("Promotion workflow spec artifact is not readable JSON.") from exc
    if not isinstance(parsed, dict):
        raise DynamicWorkflowError("Promotion workflow spec artifact must be a JSON object.")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        raise DynamicWorkflowError("Promotion workflow spec hash mismatch.")
    return cast("dict[str, object]", parsed)


def _agent_scope_owner_id(context: ToolRuntimeContext) -> str:
    execution_identity = build_execution_identity_from_runtime_context(context)
    resolved_execution = resolve_agent_execution(
        context.agent_name,
        context.config,
        execution_identity=execution_identity,
    )
    if not resolved_execution.policy.private_workspace_enabled:
        return context.agent_name
    if resolved_execution.worker_key is None:
        msg = f"Private agent '{context.agent_name}' could not resolve a Dynamic Workflow owner scope."
        raise DynamicWorkflowError(msg)
    digest = hashlib.sha256(f"{context.agent_name}\0{resolved_execution.worker_key}".encode()).hexdigest()[:24]
    return f"private_{digest}"