"""Plugin-scoped auditable Dynamic Workflow promotion implementation."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.durable_write import fsync_directory_durable, write_json_file_durable
from mindroom.dynamic_workflows.validation import DynamicWorkflowError, validate_workflow_spec
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.runtime_context import get_plugin_state_root, get_tool_runtime_context
from mindroom.tools.path_safety import resolve_base_dir_path

_PLUGIN_NAME = "dynamic-workflow-promotion"
_PROMOTION_SCHEMA_VERSION = 1
_TARGET_SCOPES = frozenset({"agent", "room", "team", "global"})
_MODES = frozenset({"dry_run", "preflight", "apply"})
_APPROVAL_ACTIONS = frozenset({"promote", "restore_previous", "tombstone", "delete"})
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_TARGET_REF_RE = re.compile(r"^[A-Za-z0-9_.:@/#-]+$")
_MATRIX_ID_RE = re.compile(r"^@[A-Za-z0-9_.=/-]+:[A-Za-z0-9.-]+$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class _PromotionConfig:
    state_root: Path
    allowed_artifact_roots: tuple[Path, ...]
    allowed_approvers: frozenset[str]
    approval_ttl: timedelta


class DynamicWorkflowPromotionTools(Toolkit):
    """Promote validated Dynamic Workflow specs into plugin-private records."""

    def __init__(self) -> None:
        super().__init__(name="dynamic_workflow_promotion")
        self.register(self.promote_dynamic_workflow_spec)

    @custom_tool_payload
    def promote_dynamic_workflow_spec(
        self,
        *,
        workflow_spec_ref: str,
        validation_ref: str,
        approval_ref: str,
        expected_workflow_sha256: str,
        expected_validation_sha256: str,
        expected_approval_sha256: str,
        target_scope: str,
        target_ref: str,
        approver: str,
        reason: str,
        mode: str = "dry_run",
        rollback_action: str = "restore_previous",
        rollback_authorization_ref: str | None = None,
    ) -> dict[str, Any]:
        """Validate and optionally persist a scoped workflow promotion record.

        The function is deliberately artifact based: callers provide immutable local JSON
        artifact references plus expected hashes. Apply mode writes only under the plugin
        state root. Dry-run and preflight execute equivalent validation without writes.
        """
        config = _load_config()
        normalized_mode = _require_choice("mode", mode, _MODES)
        normalized_scope = _require_choice("target_scope", target_scope, _TARGET_SCOPES)
        normalized_rollback = _require_choice("rollback_action", rollback_action, _APPROVAL_ACTIONS - {"promote"})
        normalized_target_ref = _validate_target_ref(target_ref)
        normalized_approver = _validate_matrix_id("approver", approver)
        normalized_reason = _validate_reason(reason)

        workflow_artifact = _read_json_artifact(config, workflow_spec_ref, expected_workflow_sha256, "workflow_spec_ref")
        validation_artifact = _read_json_artifact(config, validation_ref, expected_validation_sha256, "validation_ref")
        approval_artifact = _read_json_artifact(config, approval_ref, expected_approval_sha256, "approval_ref")

        workflow_spec = _require_object(workflow_artifact.payload, "workflow spec")
        validation_payload = _require_object(validation_artifact.payload, "validation artifact")
        approval_payload = _require_object(approval_artifact.payload, "approval evidence")

        validation_result = _validate_workflow_spec_and_artifact(
            workflow_spec=workflow_spec,
            workflow_sha256=workflow_artifact.sha256,
            validation_payload=validation_payload,
            validation_sha256=validation_artifact.sha256,
        )
        _validate_approval(
            config=config,
            approval_payload=approval_payload,
            approval_sha256=approval_artifact.sha256,
            validation_sha256=validation_artifact.sha256,
            workflow_sha256=workflow_artifact.sha256,
            target_scope=normalized_scope,
            target_ref=normalized_target_ref,
            approver=normalized_approver,
            reason=normalized_reason,
            action="promote",
        )
        rollback_authorization = _validate_rollback_policy(
            config=config,
            rollback_action=normalized_rollback,
            rollback_authorization_ref=rollback_authorization_ref,
            promotion_approval_ref=approval_artifact.resolved_ref,
            workflow_sha256=workflow_artifact.sha256,
            validation_sha256=validation_artifact.sha256,
            target_scope=normalized_scope,
            target_ref=normalized_target_ref,
            approver=normalized_approver,
        )

        promotion_id = _promotion_id(normalized_scope, normalized_target_ref)
        record = {
            "schema_version": _PROMOTION_SCHEMA_VERSION,
            "promotion_id": promotion_id,
            "target_scope": normalized_scope,
            "target_ref": normalized_target_ref,
            "workflow_spec": workflow_spec,
            "workflow_sha256": workflow_artifact.sha256,
            "workflow_spec_ref": workflow_artifact.resolved_ref,
            "validation_ref": validation_artifact.resolved_ref,
            "validation_sha256": validation_artifact.sha256,
            "approval_ref": approval_artifact.resolved_ref,
            "approval_sha256": approval_artifact.sha256,
            "approver": normalized_approver,
            "reason": normalized_reason,
            "validated_at": validation_result["validated_at"],
            "promoted_at": None,
            "rollback": {
                "action": normalized_rollback,
                "authorization_ref": rollback_authorization["authorization_ref"],
                "authorization_sha256": rollback_authorization["authorization_sha256"],
            },
        }

        response: dict[str, Any] = {
            "ok": True,
            "mode": normalized_mode,
            "would_persist": normalized_mode == "apply",
            "promotion_id": promotion_id,
            "target_scope": normalized_scope,
            "target_ref": normalized_target_ref,
            "workflow_sha256": workflow_artifact.sha256,
            "validation_sha256": validation_artifact.sha256,
            "approval_sha256": approval_artifact.sha256,
            "rollback_action": normalized_rollback,
        }
        if normalized_mode != "apply":
            return response

        applied_record = copy.deepcopy(record)
        applied_record["promoted_at"] = _now_iso()
        paths = _persist_promotion(config.state_root, promotion_id, applied_record)
        response.update(
            {
                "would_persist": True,
                "persisted": True,
                "promotion_record": str(paths["promotion_record"]),
                "audit_record": str(paths["audit_record"]),
            }
        )
        return response


@dataclass(frozen=True)
class _Artifact:
    path: Path
    resolved_ref: str
    payload: Any
    sha256: str


def _load_config() -> _PromotionConfig:
    context = get_tool_runtime_context("dynamic_workflow_promotion")
    tool_config = context.tool_config if context is not None else {}
    agent_config = context.agent_tool_config if context is not None else {}
    merged = {**tool_config, **agent_config}

    state_root_value = merged.get("state_root")
    if state_root_value:
        state_root = resolve_base_dir_path(Path.cwd(), str(state_root_value), label="state_root")
    else:
        state_root = get_plugin_state_root(_PLUGIN_NAME)
    allowed_roots = tuple(
        resolve_base_dir_path(Path.cwd(), str(root), label="allowed_artifact_roots")
        for root in _require_list(merged.get("allowed_artifact_roots", []), "allowed_artifact_roots")
    )
    allowed_approvers = frozenset(
        _validate_matrix_id("allowed_approvers[]", str(value))
        for value in _require_list(merged.get("allowed_approvers", []), "allowed_approvers")
    )
    ttl_minutes_raw = merged.get("approval_ttl_minutes", 1440)
    try:
        ttl_minutes = int(ttl_minutes_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("approval_ttl_minutes must be an integer number of minutes") from exc
    if ttl_minutes <= 0 or ttl_minutes > 525_600:
        raise ValueError("approval_ttl_minutes must be between 1 and 525600")
    return _PromotionConfig(
        state_root=state_root,
        allowed_artifact_roots=allowed_roots,
        allowed_approvers=allowed_approvers,
        approval_ttl=timedelta(minutes=ttl_minutes),
    )


def _require_list(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _read_json_artifact(config: _PromotionConfig, ref: str, expected_sha256: str, field_name: str) -> _Artifact:
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"{field_name} must be a non-empty local JSON artifact path")
    expected_hash = _validate_sha256(f"expected_{field_name}_sha256", expected_sha256)
    artifact_path = resolve_base_dir_path(Path.cwd(), ref, label=field_name)
    if artifact_path.suffix.lower() != ".json":
        raise ValueError(f"{field_name} must reference a JSON artifact")
    if config.allowed_artifact_roots and not any(
        artifact_path == root or artifact_path.is_relative_to(root) for root in config.allowed_artifact_roots
    ):
        raise ValueError(f"{field_name} is outside allowed_artifact_roots")
    data = artifact_path.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(f"{field_name} sha256 mismatch")
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must contain valid JSON") from exc
    return _Artifact(path=artifact_path, resolved_ref=str(artifact_path), payload=payload, sha256=actual_hash)


def _validate_workflow_spec_and_artifact(
    *, workflow_spec: dict[str, Any], workflow_sha256: str, validation_payload: dict[str, Any], validation_sha256: str
) -> dict[str, str]:
    try:
        validated = validate_workflow_spec(workflow_spec)
    except DynamicWorkflowError as exc:
        raise ValueError(f"workflow spec failed validation: {exc}") from exc
    _reject_forbidden_workflow_tools(validated)

    if validation_payload.get("schema_version") != _PROMOTION_SCHEMA_VERSION:
        raise ValueError("validation artifact schema_version is stale or unsupported")
    if validation_payload.get("kind") != "dynamic_workflow_validation":
        raise ValueError("validation artifact kind must be dynamic_workflow_validation")
    if validation_payload.get("workflow_sha256") != workflow_sha256:
        raise ValueError("validation artifact workflow_sha256 does not match workflow spec")
    if validation_payload.get("validation_sha256") not in (None, validation_sha256):
        raise ValueError("validation artifact self hash does not match expected validation artifact")
    if validation_payload.get("status") != "passed":
        raise ValueError("validation artifact status must be passed")
    validated_at = validation_payload.get("validated_at")
    _parse_iso_datetime(validated_at, "validation validated_at")
    validator = validation_payload.get("validator")
    if not isinstance(validator, str) or not validator.strip():
        raise ValueError("validation artifact validator is required")
    return {"validated_at": validated_at}


def _reject_forbidden_workflow_tools(workflow_spec: dict[str, Any]) -> None:
    ensure_tool_registry_loaded()
    allowed_tools: set[str] = set()
    for step in workflow_spec.get("workflow", []):
        if isinstance(step, dict):
            allowed_tools.update(_extract_step_tool_names(step))
    if not allowed_tools:
        return
    for tool_name in allowed_tools:
        if not _TOOL_NAME_RE.fullmatch(tool_name):
            raise ValueError(f"workflow references invalid tool name {tool_name!r}")
        metadata = TOOL_METADATA.get(tool_name)
        if metadata is None:
            raise ValueError(f"workflow references unknown tool {tool_name!r}")
        if tool_name in {"dynamic_workflow_promotion", "edit_config", "agent_config", "github"}:
            raise ValueError(f"workflow references forbidden promotion/configuration tool {tool_name!r}")
        if getattr(metadata, "requires_approval", False) or getattr(metadata, "requires_user_confirmation", False):
            raise ValueError(f"workflow references approval-gated tool {tool_name!r}")


def _extract_step_tool_names(step: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("tool", "tool_name", "toolkit"):
        value = step.get(key)
        if isinstance(value, str):
            names.add(value)
    tools = step.get("tools")
    if isinstance(tools, list):
        for entry in tools:
            if isinstance(entry, str):
                names.add(entry)
            elif isinstance(entry, dict):
                for key in ("name", "tool", "tool_name", "toolkit"):
                    value = entry.get(key)
                    if isinstance(value, str):
                        names.add(value)
    return names


def _validate_approval(
    *,
    config: _PromotionConfig,
    approval_payload: dict[str, Any],
    approval_sha256: str,
    validation_sha256: str,
    workflow_sha256: str,
    target_scope: str,
    target_ref: str,
    approver: str,
    reason: str,
    action: str,
) -> None:
    if approval_payload.get("schema_version") != _PROMOTION_SCHEMA_VERSION:
        raise ValueError("approval evidence schema_version is stale or unsupported")
    if approval_payload.get("kind") != "dynamic_workflow_promotion_approval":
        raise ValueError("approval evidence kind must be dynamic_workflow_promotion_approval")
    if approval_payload.get("action") != action:
        raise ValueError("approval evidence action does not authorize this operation")
    if approval_payload.get("workflow_sha256") != workflow_sha256:
        raise ValueError("approval evidence workflow_sha256 mismatch")
    if approval_payload.get("validation_sha256") != validation_sha256:
        raise ValueError("approval evidence validation_sha256 mismatch")
    if approval_payload.get("approval_sha256") not in (None, approval_sha256):
        raise ValueError("approval evidence self hash mismatch")
    if approval_payload.get("target_scope") != target_scope or approval_payload.get("target_ref") != target_ref:
        raise ValueError("approval evidence target mismatch")
    if approval_payload.get("approver") != approver:
        raise ValueError("approval evidence approver mismatch")
    if config.allowed_approvers and approver not in config.allowed_approvers:
        raise ValueError("approver is not in allowed_approvers")
    if approval_payload.get("requester") == approver:
        raise ValueError("self-promotion is not allowed")
    if approval_payload.get("reason") != reason:
        raise ValueError("approval evidence reason mismatch")
    if approval_payload.get("redacted") is True or approval_payload.get("revoked") is True:
        raise ValueError("approval evidence has been redacted or revoked")
    nonce = approval_payload.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise ValueError("approval evidence nonce must be at least 16 characters")
    approved_at = _parse_iso_datetime(approval_payload.get("approved_at"), "approval approved_at")
    if approved_at + config.approval_ttl < datetime.now(UTC):
        raise ValueError("approval evidence has expired")


def _validate_rollback_policy(
    *,
    config: _PromotionConfig,
    rollback_action: str,
    rollback_authorization_ref: str | None,
    promotion_approval_ref: str,
    workflow_sha256: str,
    validation_sha256: str,
    target_scope: str,
    target_ref: str,
    approver: str,
) -> dict[str, str | None]:
    if rollback_action == "restore_previous" and rollback_authorization_ref is None:
        return {"authorization_ref": None, "authorization_sha256": None}
    if not rollback_authorization_ref:
        raise ValueError(f"rollback_action {rollback_action!r} requires rollback_authorization_ref")
    authorization = _read_json_artifact(config, rollback_authorization_ref, _hash_file_ref(rollback_authorization_ref), "rollback_authorization_ref")
    if authorization.resolved_ref == promotion_approval_ref:
        raise ValueError("rollback authorization must not reuse promotion approval evidence")
    payload = _require_object(authorization.payload, "rollback authorization evidence")
    _validate_approval(
        config=config,
        approval_payload=payload,
        approval_sha256=authorization.sha256,
        validation_sha256=validation_sha256,
        workflow_sha256=workflow_sha256,
        target_scope=target_scope,
        target_ref=target_ref,
        approver=approver,
        reason=str(payload.get("reason", "")),
        action=rollback_action,
    )
    return {"authorization_ref": authorization.resolved_ref, "authorization_sha256": authorization.sha256}


def _hash_file_ref(ref: str) -> str:
    path = resolve_base_dir_path(Path.cwd(), ref, label="rollback_authorization_ref")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _persist_promotion(state_root: Path, promotion_id: str, record: dict[str, Any]) -> dict[str, Path]:
    promotions_dir = state_root / "promotions"
    audit_dir = state_root / "audit"
    lock_path = state_root / ".promotion.lock"
    state_root.mkdir(parents=True, exist_ok=True)
    with _file_lock(lock_path):
        promotions_dir.mkdir(parents=True, exist_ok=True)
        audit_dir.mkdir(parents=True, exist_ok=True)
        promotion_path = promotions_dir / f"{promotion_id}.json"
        audit_id = f"{_compact_timestamp()}-{uuid.uuid4().hex}"
        audit_path = audit_dir / f"{promotion_id}-{audit_id}.json"
        existing_record = _read_existing_record(promotion_path)
        durable_record = copy.deepcopy(record)
        durable_record["previous_workflow_sha256"] = existing_record.get("workflow_sha256") if existing_record else None
        audit_record = {
            "schema_version": _PROMOTION_SCHEMA_VERSION,
            "kind": "dynamic_workflow_promotion_audit",
            "audit_id": audit_id,
            "promotion_id": promotion_id,
            "target_scope": durable_record["target_scope"],
            "target_ref": durable_record["target_ref"],
            "workflow_sha256": durable_record["workflow_sha256"],
            "validation_sha256": durable_record["validation_sha256"],
            "approval_sha256": durable_record["approval_sha256"],
            "previous_workflow_sha256": durable_record["previous_workflow_sha256"],
            "approver": durable_record["approver"],
            "reason": durable_record["reason"],
            "promoted_at": durable_record["promoted_at"],
        }
        write_json_file_durable(audit_path, audit_record)
        write_json_file_durable(promotion_path, durable_record)
        fsync_directory_durable(promotions_dir)
        fsync_directory_durable(audit_dir)
        fsync_directory_durable(state_root)
    return {"promotion_record": promotion_path, "audit_record": audit_path}


def _read_existing_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("existing promotion record is not valid JSON; refusing overwrite") from exc
    return _require_object(payload, "existing promotion record")


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _promotion_id(scope: str, target_ref: str) -> str:
    digest = hashlib.sha256(f"{scope}\0{target_ref}".encode("utf-8")).hexdigest()[:16]
    safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_ref).strip("-")[:48] or "target"
    return f"{scope}-{safe_ref}-{digest}"


def _validate_sha256(name: str, value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _validate_target_ref(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("target_ref is required")
    normalized = value.strip()
    if len(normalized) > 200 or not _TARGET_REF_RE.fullmatch(normalized):
        raise ValueError("target_ref contains unsupported characters")
    return normalized


def _validate_matrix_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _MATRIX_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a full Matrix ID")
    return value


def _validate_reason(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("reason must be a string")
    normalized = value.strip()
    if len(normalized) < 12 or len(normalized) > 2000:
        raise ValueError("reason must be between 12 and 2000 characters")
    return normalized


def _require_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return normalized


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _parse_iso_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _compact_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")