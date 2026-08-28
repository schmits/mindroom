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
_TARGET_SCOPES = frozenset({"agent", "room", "thread", "tenant"})
_ROLLBACK_ACTIONS = frozenset({"disable", "restore_previous", "tombstone", "delete"})
_REVOKED_APPROVAL_STATUSES = frozenset({"revoked", "redacted", "denied", "expired"})
_REF_RE = re.compile(r"^[^\x00]{1,4096}$")
_REASON_RE = re.compile(r"^[^\x00]{1,2048}$")
_MATRIX_USER_RE = re.compile(r"^@[A-Za-z0-9_.=/-]+:[A-Za-z0-9.-]+$")
_MATRIX_ROOM_RE = re.compile(r"^![^:]+:[A-Za-z0-9.-]+$")
_MATRIX_EVENT_RE = re.compile(r"^\$[^:]+:[A-Za-z0-9.-]+$")
_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_FORBIDDEN_WORKFLOW_TOOLS = frozenset(
    {
        "claude_agent",
        "config_manager",
        "delegate",
        "dynamic_tools",
        "dynamic_workflow",
        "dynamic_workflow_promotion",
        "github",
        "invite_router",
        "memory",
        "repo_workspace",
        "scheduler",
        "self_config",
        "subagents",
    }
)


@dataclass(frozen=True)
class PromotionInput:
    workflow_spec_ref: str
    validated_artifact_ref: str
    target_scope: str
    target_ref: str
    approved_by: str
    approval_evidence_ref: str
    expected_spec_hash: str
    dry_run: bool
    preflight: bool
    rollback_policy: dict[str, object]
    reason: str


class DynamicWorkflowPromotionTools(Toolkit):
    """Persist hash-bound Dynamic Workflow promotion records and immutable audit."""

    def __init__(
        self,
        state_root: str | None = None,
        allowed_artifact_roots: list[str] | str | None = None,
        allowed_approvers: list[str] | str | None = None,
        approval_ttl_minutes: int = 1440,
        self_actor_ids: list[str] | str | None = None,
    ) -> None:
        self._state_root_override = state_root
        self._allowed_artifact_roots = _normalize_roots(allowed_artifact_roots)
        self._allowed_approvers = frozenset(_normalize_string_list(allowed_approvers))
        self._approval_ttl_minutes = _validate_ttl(approval_ttl_minutes)
        self._self_actor_ids = frozenset(_normalize_string_list(self_actor_ids))
        super().__init__(name="dynamic_workflow_promotion", tools=[self.promote_dynamic_workflow_spec])

    def promote_dynamic_workflow_spec(
        self,
        workflow_spec_ref: str,
        validated_artifact_ref: str,
        target_scope: str,
        target_ref: str,
        approved_by: str,
        approval_evidence_ref: str,
        expected_spec_hash: str,
        dry_run: bool,
        preflight: bool,
        rollback_policy: dict[str, Any],
        reason: str,
    ) -> str:
        """Promote one validated Dynamic Workflow spec with hash-bound approval evidence."""
        try:
            request = _promotion_input(
                workflow_spec_ref=workflow_spec_ref,
                validated_artifact_ref=validated_artifact_ref,
                target_scope=target_scope,
                target_ref=target_ref,
                approved_by=approved_by,
                approval_evidence_ref=approval_evidence_ref,
                expected_spec_hash=expected_spec_hash,
                dry_run=dry_run,
                preflight=preflight,
                rollback_policy=rollback_policy,
                reason=reason,
            )
            result = self._promote(request)
        except (OSError, ValueError, DynamicWorkflowError) as exc:
            return _payload("error", message=str(exc))
        return _payload("ok", **result)

    def _promote(self, request: PromotionInput) -> dict[str, object]:
        context = get_tool_runtime_context()
        if context is None:
            raise ValueError("Dynamic Workflow promotion requires an active tool runtime context.")
        if _is_self_approval(request.approved_by, context, configured_self_actor_ids=self._self_actor_ids):
            raise ValueError("Self-promotion is not allowed.")
        if self._allowed_approvers and request.approved_by not in self._allowed_approvers:
            raise ValueError("Approval evidence approver is not allowed for this promotion tool configuration.")

        state_root = self._state_root(context)
        workflow_spec, spec_bytes, spec_hash = self._read_json_artifact(
            request.workflow_spec_ref,
            description="workflow spec",
            storage_root=context.runtime_paths.storage_root,
        )
        if spec_hash != request.expected_spec_hash:
            raise ValueError("Spec substitution detected: expected_spec_hash does not match workflow_spec_ref content.")
        validated_spec = validate_workflow_spec(workflow_spec)
        _validate_workflow_tools(validated_spec, runtime_paths=context.runtime_paths)
        validation_artifact = self._read_json_object_artifact(request.validated_artifact_ref, description="validation artifact")
        approval_evidence = self._read_json_object_artifact(request.approval_evidence_ref, description="approval evidence")
        validation_hash = _sha256_json(validation_artifact)
        approval_hash = _sha256_json(approval_evidence)
        target = _target_identity(request.target_scope, request.target_ref)
        workflow_id = str(validated_spec["id"])

        _validate_validation_artifact(
            validation_artifact,
            workflow_id=workflow_id,
            spec_hash=spec_hash,
            target_scope=request.target_scope,
            target_ref=request.target_ref,
            approved_by=request.approved_by,
        )
        _validate_approval_evidence(
            approval_evidence,
            workflow_id=workflow_id,
            spec_hash=spec_hash,
            target_scope=request.target_scope,
            target_ref=request.target_ref,
            approved_by=request.approved_by,
            reason=request.reason,
            ttl_minutes=self._approval_ttl_minutes,
        )
        rollback = _validate_rollback_policy(request.rollback_policy, expected_spec_hash=spec_hash)
        rollback_authorization = None
        rollback_authorization_hash = None
        if rollback.get("authorization_ref") is not None:
            rollback_authorization = self._read_json_object_artifact(
                cast("str", rollback["authorization_ref"]),
                description="rollback authorization",
            )
            rollback_authorization_hash = _sha256_json(rollback_authorization)
            _validate_rollback_authorization(
                rollback_authorization,
                rollback,
                workflow_id=workflow_id,
                target_scope=request.target_scope,
                target_ref=request.target_ref,
                approved_by=request.approved_by,
                ttl_minutes=self._approval_ttl_minutes,
            )
        promotion_id = _promotion_id(request.target_scope, request.target_ref, workflow_id)
        promotion_record = _promotion_record(
            promotion_id=promotion_id,
            request=request,
            workflow_id=workflow_id,
            workflow_name=str(validated_spec["name"]),
            spec_hash=spec_hash,
            spec_bytes_len=len(spec_bytes),
            validation_hash=validation_hash,
            approval_hash=approval_hash,
            target=target,
            rollback=rollback,
            rollback_authorization_hash=rollback_authorization_hash,
            actor=context.agent_name,
            requester_id=context.requester_id,
        )
        if request.preflight or request.dry_run:
            audit_record = _audit_record(promotion_record, action="preflight" if request.preflight else "dry_run")
            return {
                "mode": "dry_run" if request.dry_run else "preflight",
                "persisted": False,
                "promotion_id": promotion_id,
                "workflow_id": workflow_id,
                "spec_hash": spec_hash,
                "target": target,
                "audit_hash": _sha256_json(audit_record),
                "message": "Promotion preflight passed; no state was persisted.",
            }

        return self._persist_apply(state_root, promotion_id, promotion_record)

    def _persist_apply(
        self,
        state_root: Path,
        promotion_id: str,
        promotion_record: dict[str, object],
    ) -> dict[str, object]:
        with _state_lock(state_root):
            current_path = state_root / "promotions" / f"{promotion_id}.json"
            existing = _load_json_object(current_path) if current_path.exists() else None
            if existing is not None:
                existing_hash = cast("str | None", existing.get("spec_hash"))
                if existing_hash == promotion_record["spec_hash"] and existing.get("status") == "active":
                    raise ValueError("Approval replay detected: this promotion is already active for the same spec hash and scope.")
                existing_hash = _sha256_json(existing)
                if _rollback_is_abuse(cast("dict[str, object]", promotion_record["rollback_policy"]), existing):
                    raise ValueError("Rollback abuse detected: restore/delete actions require previous promotion authorization.")
                _validate_rollback_previous_binding(cast("dict[str, object]", promotion_record["rollback_policy"]), existing_hash)
                promotion_record["previous_promotion_hash"] = existing_hash
            else:
                promotion_record["previous_promotion_hash"] = None
                _validate_rollback_previous_binding(cast("dict[str, object]", promotion_record["rollback_policy"]), None)
            audit_record = _audit_record(promotion_record, action="apply")
            audit_hash = _sha256_json(audit_record)
            audit_record["audit_hash"] = audit_hash
            audit_path = state_root / "audit" / f"{_utc_stamp()}-{promotion_id}-{audit_hash[:12]}.json"
            write_json_file_durable(
                audit_path,
                audit_record,
                indent=2,
                sort_keys=True,
                trailing_newline=True,
                strict_atomic_replace=True,
            )
            fsync_directory_durable(state_root / "audit")
            if not audit_path.exists():
                raise ValueError("Audit failure: audit record was not durably written.")
            write_json_file_durable(
                current_path,
                promotion_record,
                indent=2,
                sort_keys=True,
                trailing_newline=True,
                strict_atomic_replace=True,
            )
            if not current_path.exists():
                raise ValueError("Audit failure: promotion record was not durably written.")
        return {
            "mode": "apply",
            "persisted": True,
            "promotion_id": promotion_id,
            "workflow_id": promotion_record["workflow_id"],
            "spec_hash": promotion_record["spec_hash"],
            "target": promotion_record["target"],
            "audit_hash": audit_hash,
            "promotion_record_ref": str(current_path),
            "audit_record_ref": str(audit_path),
        }

    def _state_root(self, context: object) -> Path:
        if self._state_root_override:
            root = Path(self._state_root_override).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            return root
        return get_plugin_state_root(_PLUGIN_NAME)

    def _read_json_object_artifact(self, artifact_ref: str, *, description: str) -> dict[str, object]:
        context = get_tool_runtime_context()
        if context is None:
            raise ValueError("Dynamic Workflow promotion requires an active tool runtime context.")
        data, _bytes, _digest = self._read_json_artifact(
            artifact_ref,
            description=description,
            storage_root=context.runtime_paths.storage_root,
        )
        return data

    def _read_json_artifact(
        self,
        artifact_ref: str,
        *,
        description: str,
        storage_root: Path,
    ) -> tuple[dict[str, object], bytes, str]:
        path = _resolve_artifact_ref(
            artifact_ref,
            self._allowed_artifact_roots,
            storage_root=storage_root,
            description=description,
        )
        raw = path.read_bytes()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"{description} artifact must be UTF-8 JSON.") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{description} artifact must contain a JSON object.")
        return cast("dict[str, object]", parsed), raw, _sha256_bytes(raw)


def _payload(status: str, **fields: object) -> str:
    return custom_tool_payload("dynamic_workflow_promotion", status, **fields)


def _promotion_input(**kwargs: object) -> PromotionInput:
    required = (
        "workflow_spec_ref",
        "validated_artifact_ref",
        "target_scope",
        "target_ref",
        "approved_by",
        "approval_evidence_ref",
        "expected_spec_hash",
        "dry_run",
        "preflight",
        "rollback_policy",
        "reason",
    )
    for field in required:
        if field not in kwargs:
            raise ValueError(f"Missing required promotion input '{field}'.")
    text_fields = {field: _required_ref_text(cast("str", kwargs[field]), field) for field in required[:7]}
    reason = _required_reason(cast("str", kwargs["reason"]))
    if len(reason) < 8:
        raise ValueError("Promotion reason must be at least 8 characters.")
    target_scope = text_fields["target_scope"]
    if target_scope not in _TARGET_SCOPES:
        raise ValueError("Ambiguous scope: target_scope must be one of agent, room, thread, tenant.")
    expected_spec_hash = _normalize_hash(text_fields["expected_spec_hash"], "expected_spec_hash")
    approved_by = text_fields["approved_by"]
    if not _MATRIX_USER_RE.fullmatch(approved_by):
        raise ValueError("approved_by must be a full Matrix user ID.")
    rollback_policy = kwargs["rollback_policy"]
    if not isinstance(rollback_policy, dict):
        raise ValueError("rollback_policy must be a JSON object.")
    dry_run = kwargs["dry_run"]
    preflight = kwargs["preflight"]
    if not isinstance(dry_run, bool) or not isinstance(preflight, bool):
        raise ValueError("dry_run and preflight must be booleans.")
    return PromotionInput(
        workflow_spec_ref=text_fields["workflow_spec_ref"],
        validated_artifact_ref=text_fields["validated_artifact_ref"],
        target_scope=target_scope,
        target_ref=text_fields["target_ref"],
        approved_by=approved_by,
        approval_evidence_ref=text_fields["approval_evidence_ref"],
        expected_spec_hash=expected_spec_hash,
        dry_run=dry_run,
        preflight=preflight,
        rollback_policy=cast("dict[str, object]", rollback_policy),
        reason=reason,
    )


def _required_ref_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    normalized = value.strip()
    if not _REF_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsupported characters or is too long.")
    return normalized


def _required_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reason must be a non-empty string.")
    normalized = value.strip()
    if not _REASON_RE.fullmatch(normalized):
        raise ValueError("reason contains unsupported characters or is too long.")
    return normalized


def _normalize_hash(value: str, field_name: str) -> str:
    if not _HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a sha256 digest.")
    return value.removeprefix("sha256:").lower()


def _resolve_artifact_ref(
    artifact_ref: str,
    allowed_roots: tuple[Path, ...],
    *,
    storage_root: Path,
    description: str,
) -> Path:
    """Resolve a durable promotion artifact reference.

    ``artifact://<relative-path>`` is the canonical shared-artifact scheme. It
    resolves beneath ``<storage_root>/artifacts`` so preflight callers cannot
    accidentally bind tenant/room promotion to an agent-local workspace path.
    Absolute local paths remain available only for explicitly configured plugin
    roots, which keeps existing guarded deployments working without accepting
    Toolsmith-local relative paths as durable references.
    """
    if description == "workflow spec" and artifact_ref.lower().endswith((".md", ".markdown")):
        raise ValueError("workflow spec artifact ref must point to canonical workflow spec JSON, not Markdown.")
    if artifact_ref.startswith("artifact://"):
        path = _resolve_shared_artifact_ref(artifact_ref, storage_root=storage_root, description=description)
    elif "://" in artifact_ref:
        raise ValueError(f"{description} artifact ref must use artifact:// or an allowed absolute local path.")
    else:
        candidate = Path(artifact_ref).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                f"{description} artifact ref must use artifact:// shared artifacts or an allowed absolute local path."
            )
        path = candidate.resolve()
        if allowed_roots:
            for root in allowed_roots:
                try:
                    path = resolve_base_dir_path(root, str(path))
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"{description} artifact ref is outside configured allowed_artifact_roots.")
        else:
            raise ValueError(
                f"{description} artifact ref must use artifact:// shared artifacts "
                "or be under configured allowed_artifact_roots."
            )
    if description == "workflow spec" and path.suffix.lower() != ".json":
        raise ValueError("workflow spec artifact must be canonical JSON.")
    if not path.is_file():
        raise ValueError(f"{description} artifact ref does not point to a readable file.")
    return path


def _resolve_shared_artifact_ref(artifact_ref: str, *, storage_root: Path, description: str) -> Path:
    relative = artifact_ref.removeprefix("artifact://")
    if not relative or relative.startswith("/"):
        raise ValueError(f"{description} artifact ref must include a relative artifact path.")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"{description} artifact ref must stay within the shared artifact namespace.")
    artifacts_root = (storage_root / "artifacts").resolve()
    return resolve_base_dir_path(artifacts_root, relative_path.as_posix())


def _normalize_roots(value: list[str] | str | None) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser().resolve() for item in _normalize_string_list(value))


def _normalize_string_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Expected a list of non-empty strings.")
        normalized.append(item.strip())
    return normalized


def _validate_ttl(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 10080:
        raise ValueError("approval_ttl_minutes must be an integer between 1 and 10080.")
    return value


def _validate_workflow_tools(spec: dict[str, object], *, runtime_paths: object) -> None:
    ensure_tool_registry_loaded(runtime_paths)  # type: ignore[arg-type]
    permissions = spec.get("permissions")
    permission_tools = _string_set(cast("dict[str, object]", permissions).get("tools")) if isinstance(permissions, dict) else set()
    participants = spec.get("participants")
    participant_tools: set[str] = set()
    if isinstance(participants, list):
        for participant in participants:
            if isinstance(participant, dict):
                participant_tools.update(_string_set(participant.get("tools")))
    all_tools = permission_tools | participant_tools
    for tool in sorted(all_tools):
        if tool == "*" or tool in _FORBIDDEN_WORKFLOW_TOOLS:
            raise ValueError(f"Forbidden Dynamic Workflow tool grant: {tool}.")
        if tool not in TOOL_METADATA:
            raise ValueError(f"Stale schema or unknown Dynamic Workflow tool grant: {tool}.")


def _string_set(value: object) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ValueError("Workflow tool grants must be lists.")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Workflow tool grants must be non-empty strings.")
        result.add(item.strip())
    return result


def _validate_validation_artifact(
    artifact: dict[str, object],
    *,
    workflow_id: str,
    spec_hash: str,
    target_scope: str,
    target_ref: str,
    approved_by: str,
) -> None:
    if artifact.get("schema_version") != _PROMOTION_SCHEMA_VERSION:
        raise ValueError("Stale schema: validation artifact schema_version mismatch.")
    if artifact.get("status") not in {"validated", "ok"}:
        raise ValueError("Validation artifact is not successful.")
    _require_equal(artifact, "workflow_id", workflow_id, "validation artifact workflow_id mismatch")
    _require_equal(artifact, "spec_hash", spec_hash, "validation artifact spec_hash mismatch")
    _require_equal(artifact, "target_scope", target_scope, "validation artifact target_scope mismatch")
    _require_equal(artifact, "target_ref", target_ref, "validation artifact target_ref mismatch")
    allowed_approver = artifact.get("approved_by")
    if allowed_approver is not None and allowed_approver != approved_by:
        raise ValueError("Validation artifact approved_by mismatch.")


def _validate_approval_evidence(
    evidence: dict[str, object],
    *,
    workflow_id: str,
    spec_hash: str,
    target_scope: str,
    target_ref: str,
    approved_by: str,
    reason: str,
    ttl_minutes: int,
) -> None:
    if evidence.get("schema_version") != _PROMOTION_SCHEMA_VERSION:
        raise ValueError("Stale schema: approval evidence schema_version mismatch.")
    if evidence.get("status") != "approved":
        if evidence.get("status") in _REVOKED_APPROVAL_STATUSES:
            raise ValueError("Approval is expired, revoked, redacted, denied, or otherwise not active.")
        raise ValueError("Approval evidence is not approved.")
    _require_equal(evidence, "workflow_id", workflow_id, "approval evidence workflow_id mismatch")
    _require_equal(evidence, "spec_hash", spec_hash, "approval evidence spec_hash mismatch")
    _require_equal(evidence, "target_scope", target_scope, "approval evidence target_scope mismatch")
    _require_equal(evidence, "target_ref", target_ref, "approval evidence target_ref mismatch")
    _require_equal(evidence, "approved_by", approved_by, "approval evidence approved_by mismatch")
    if evidence.get("reason") not in {None, reason}:
        raise ValueError("Approval evidence reason mismatch.")
    event_id = evidence.get("event_id")
    if not isinstance(event_id, str) or not _MATRIX_EVENT_RE.fullmatch(event_id):
        raise ValueError("Approval evidence must include an unambiguous Matrix event_id.")
    if evidence.get("redacted") is True or evidence.get("revoked") is True:
        raise ValueError("Approval is revoked or redacted.")
    approved_at = _parse_time(cast("str | None", evidence.get("approved_at")))
    expires_at = _parse_time(cast("str | None", evidence.get("expires_at")))
    now = datetime.now(UTC)
    if approved_at is None:
        raise ValueError("Approval evidence must include approved_at.")
    if approved_at > now + timedelta(minutes=5):
        raise ValueError("Approval evidence approved_at is in the future.")
    if now - approved_at > timedelta(minutes=ttl_minutes):
        raise ValueError("Approval evidence is expired.")
    if expires_at is not None and now >= expires_at:
        raise ValueError("Approval evidence is expired.")


def _validate_rollback_policy(policy: dict[str, object], *, expected_spec_hash: str) -> dict[str, object]:
    action = policy.get("action")
    if action not in _ROLLBACK_ACTIONS:
        raise ValueError("rollback_policy.action must be disable, restore_previous, tombstone, or delete.")
    rollback_hash = policy.get("expected_spec_hash")
    if rollback_hash is not None and _normalize_hash(cast("str", rollback_hash), "rollback_policy.expected_spec_hash") != expected_spec_hash:
        raise ValueError("rollback_policy expected_spec_hash mismatch.")
    authorization_ref = policy.get("authorization_ref")
    if action in {"restore_previous", "tombstone", "delete"} and not isinstance(authorization_ref, str):
        raise ValueError("Rollback action requires equal-or-stronger authorization_ref.")
    return copy.deepcopy(policy)


def _validate_rollback_authorization(
    authorization: dict[str, object],
    policy: dict[str, object],
    *,
    workflow_id: str,
    target_scope: str,
    target_ref: str,
    approved_by: str,
    ttl_minutes: int,
) -> None:
    action = policy.get("action")
    if action not in {"restore_previous", "tombstone", "delete"}:
        return
    if authorization.get("schema_version") != _PROMOTION_SCHEMA_VERSION:
        raise ValueError("Stale schema: rollback authorization schema_version mismatch.")
    if authorization.get("status") != "approved":
        if authorization.get("status") in _REVOKED_APPROVAL_STATUSES:
            raise ValueError("Rollback authorization is expired, revoked, redacted, denied, or otherwise not active.")
        raise ValueError("Rollback authorization is not approved.")
    if authorization.get("redacted") is True or authorization.get("revoked") is True:
        raise ValueError("Rollback authorization is revoked or redacted.")
    _require_equal(authorization, "workflow_id", workflow_id, "rollback authorization workflow_id mismatch")
    _require_equal(authorization, "target_scope", target_scope, "rollback authorization target_scope mismatch")
    _require_equal(authorization, "target_ref", target_ref, "rollback authorization target_ref mismatch")
    _require_equal(authorization, "approved_by", approved_by, "rollback authorization approved_by mismatch")
    authorized_action = authorization.get("rollback_action")
    authorized_actions = authorization.get("rollback_actions")
    if authorized_action != action and not (isinstance(authorized_actions, list) and action in authorized_actions):
        raise ValueError("Rollback authorization action mismatch.")
    expected_previous = policy.get("expected_previous_promotion_hash")
    authorized_previous = authorization.get("previous_promotion_hash")
    if not isinstance(expected_previous, str) or not isinstance(authorized_previous, str):
        raise ValueError("Rollback authorization requires previous_promotion_hash binding.")
    if _normalize_hash(authorized_previous, "rollback_authorization.previous_promotion_hash") != _normalize_hash(
        expected_previous, "rollback_policy.expected_previous_promotion_hash"
    ):
        raise ValueError("Rollback authorization previous_promotion_hash mismatch.")
    event_id = authorization.get("event_id")
    if not isinstance(event_id, str) or not _MATRIX_EVENT_RE.fullmatch(event_id):
        raise ValueError("Rollback authorization must include an unambiguous Matrix event_id.")
    approved_at = _parse_time(cast("str | None", authorization.get("approved_at")))
    expires_at = _parse_time(cast("str | None", authorization.get("expires_at")))
    now = datetime.now(UTC)
    if approved_at is None:
        raise ValueError("Rollback authorization must include approved_at.")
    if approved_at > now + timedelta(minutes=5):
        raise ValueError("Rollback authorization approved_at is in the future.")
    if now - approved_at > timedelta(minutes=ttl_minutes):
        raise ValueError("Rollback authorization is expired.")
    if expires_at is not None and now >= expires_at:
        raise ValueError("Rollback authorization is expired.")


def _validate_rollback_previous_binding(policy: dict[str, object], previous_promotion_hash: str | None) -> None:
    action = policy.get("action")
    if action not in {"restore_previous", "tombstone", "delete"}:
        return
    expected_previous = policy.get("expected_previous_promotion_hash")
    if previous_promotion_hash is None:
        raise ValueError("Rollback action requires an existing active promotion to bind authorization.")
    if not isinstance(expected_previous, str):
        raise ValueError("Rollback action requires expected_previous_promotion_hash binding.")
    if _normalize_hash(expected_previous, "rollback_policy.expected_previous_promotion_hash") != previous_promotion_hash:
        raise ValueError("rollback_policy expected_previous_promotion_hash mismatch.")


def _rollback_is_abuse(policy: dict[str, object], existing: dict[str, object]) -> bool:
    action = policy.get("action")
    if action not in {"restore_previous", "tombstone", "delete"}:
        return False
    return policy.get("authorization_ref") in {None, existing.get("approval_evidence_ref")}


def _target_identity(scope: str, target_ref: str) -> dict[str, str]:
    if scope == "room" and not _MATRIX_ROOM_RE.fullmatch(target_ref):
        raise ValueError("room target_ref must be a full Matrix room ID.")
    if scope == "thread":
        room_id, sep, event_id = target_ref.partition("#")
        if sep != "#" or not _MATRIX_ROOM_RE.fullmatch(room_id) or not _MATRIX_EVENT_RE.fullmatch(event_id):
            raise ValueError("thread target_ref must be !room:server#$event:server.")
    if scope == "tenant" and target_ref != "tenant":
        raise ValueError("tenant target_ref must be 'tenant'.")
    if scope == "agent" and not target_ref:
        raise ValueError("agent target_ref must be non-empty.")
    return {"scope": scope, "ref": target_ref, "hash": _sha256_text(f"{scope}\0{target_ref}")}


def _promotion_id(scope: str, target_ref: str, workflow_id: str) -> str:
    return _sha256_text(f"{scope}\0{target_ref}\0{workflow_id}")[:32]


def _promotion_record(**fields: object) -> dict[str, object]:
    request = cast("PromotionInput", fields.pop("request"))
    return {
        "schema_version": _PROMOTION_SCHEMA_VERSION,
        "promotion_id": fields["promotion_id"],
        "workflow_id": fields["workflow_id"],
        "workflow_name": fields["workflow_name"],
        "status": "active",
        "target_scope": request.target_scope,
        "target_ref": request.target_ref,
        "target": fields["target"],
        "spec_hash": fields["spec_hash"],
        "spec_bytes_len": fields["spec_bytes_len"],
        "workflow_spec_ref": request.workflow_spec_ref,
        "validated_artifact_ref": request.validated_artifact_ref,
        "validation_artifact_hash": fields["validation_hash"],
        "approval_evidence_ref": request.approval_evidence_ref,
        "approval_evidence_hash": fields["approval_hash"],
        "approved_by": request.approved_by,
        "reason": request.reason,
        "rollback_policy": fields["rollback"],
        "rollback_authorization_hash": fields["rollback_authorization_hash"],
        "created_at": _iso_now(),
        "created_by": fields["actor"],
        "requester_id": fields["requester_id"],
        "operation_id": f"dwp_{uuid.uuid4().hex}",
    }


def _audit_record(promotion_record: dict[str, object], *, action: str) -> dict[str, object]:
    return {
        "schema_version": _PROMOTION_SCHEMA_VERSION,
        "audit_id": f"audit_{uuid.uuid4().hex}",
        "action": action,
        "record_hash": _sha256_json(promotion_record),
        "promotion_id": promotion_record["promotion_id"],
        "workflow_id": promotion_record["workflow_id"],
        "target_scope": promotion_record["target_scope"],
        "target_ref": promotion_record["target_ref"],
        "spec_hash": promotion_record["spec_hash"],
        "approved_by": promotion_record["approved_by"],
        "reason": promotion_record["reason"],
        "operation_id": promotion_record["operation_id"],
        "created_by": promotion_record.get("created_by"),
        "requester_id": promotion_record.get("requester_id"),
        "recorded_at": _iso_now(),
    }


def _actor_self_approval_ids(context: object, *, configured_self_actor_ids: frozenset[str]) -> set[str]:
    identities = set(configured_self_actor_ids)
    for attr in (
        "agent_name",
        "agent_id",
        "actor_id",
        "actor_name",
        "matrix_id",
        "user_id",
        "sender_id",
    ):
        value = getattr(context, attr, None)
        if isinstance(value, str) and value.strip():
            identities.add(value.strip())
    return identities


def _is_self_approval(approved_by: str, context: object, *, configured_self_actor_ids: frozenset[str]) -> bool:
    return approved_by in _actor_self_approval_ids(context, configured_self_actor_ids=configured_self_actor_ids)


def _require_equal(data: dict[str, object], key: str, expected: object, message: str) -> None:
    if data.get(key) != expected:
        raise ValueError(message)


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ValueError("Approval evidence timestamp is invalid.") from exc


def _load_json_object(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Existing promotion record is invalid.")
    return cast("dict[str, object]