"""Ephemeral repository workspace tools with strict path confinement.

This toolkit is intentionally a file/diff substrate, not a shell, package
manager, or GitHub publishing tool. It creates repo-scoped workspaces, records
provenance metadata, confines file access to the workspace repo directory, and
produces diffs/artifacts that other tools can consume.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agno.tools import Toolkit


_DEFAULT_WORKSPACE_ROOT = Path(os.environ.get("MINDROOM_REPO_WORKSPACE_ROOT", "/tmp/mindroom_repo_workspaces"))
_DEFAULT_TTL_MINUTES = 120
_MAX_FILE_BYTES = 1_000_000
_ALLOWED_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OWNER_WILDCARD_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/\*$")
_DENIED_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+|\*)$")
_SAFE_GIT_CONFIG_ARGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "diff.trustExitCode=false",
)


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    repo: Path
    artifacts: Path
    metadata: Path


class RepoWorkspaceTools(Toolkit):
    """Ephemeral repo-scoped workspace management without command execution."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        allowed_repos: list[str] | str | None = None,
        denied_repos: list[str] | str | None = None,
        allowed_source_roots: list[str] | str | None = None,
        default_ttl_minutes: int = _DEFAULT_TTL_MINUTES,
        max_file_bytes: int = _MAX_FILE_BYTES,
        **kwargs: Any,
    ) -> None:
        self.workspace_root = Path(workspace_root or _DEFAULT_WORKSPACE_ROOT).resolve()
        self.allowed_repos = _validate_repo_patterns(_normalize_string_list(allowed_repos), field="allowed_repos")
        self.denied_repos = _validate_repo_patterns(_normalize_string_list(denied_repos), field="denied_repos")
        self.allowed_source_roots = [
            Path(item).resolve() for item in _normalize_string_list(allowed_source_roots)
        ]
        self.default_ttl_minutes = default_ttl_minutes
        self.max_file_bytes = max_file_bytes
        super().__init__(name="repo_workspace", **kwargs)
        self.register(self.create_workspace)
        self.register(self.get_workspace_info)
        self.register(self.list_files)
        self.register(self.read_file)
        self.register(self.write_file)
        self.register(self.apply_patch)
        self.register(self.get_status)
        self.register(self.get_diff)
        self.register(self.export_patch)
        self.register(self.handoff_to_coding_sandbox)
        self.register(self.destroy_workspace)

    def create_workspace(
        self,
        repo: str = "schmits/repo-sandbox-fixture",
        ref: str | None = None,
        source_path: str | None = None,
        workspace_id: str | None = None,
        ttl_minutes: int | None = None,
        allow_network: bool = False,
        confirm_write: bool = False,
    ) -> str:
        """Create an ephemeral workspace.

        Args:
            repo: GitHub repository identity in ``owner/name`` form. It is used for policy checks
                and provenance only; this tool does not clone or fetch.
            ref: Optional provenance ref/SHA/branch label. It is recorded, not checked out.
            source_path: Optional local directory to copy into the workspace repo directory.
                If provided, it must be inside one of ``allowed_source_roots``.
            workspace_id: Optional caller-supplied ID. Generated IDs avoid collisions.
            ttl_minutes: Workspace lifetime, after which status marks it expired.
            allow_network: Must remain false. Network operations are intentionally unsupported.
            confirm_write: Must be true because this creates directories/files.
        """
        if not confirm_write:
            return _write_confirmation_error("create_workspace")
        if allow_network:
            return "Error: repo_workspace does not perform network operations; provide a pre-seeded local source_path instead."
        try:
            repo_name = _validate_repo_name(repo)
            self._check_repo_policy(repo_name)
            ttl = self._validate_ttl(ttl_minutes)
            workspace_name = _validate_workspace_id(workspace_id or f"rw-{uuid.uuid4().hex[:12]}")
            paths = self._paths(workspace_name)
            if paths.root.exists():
                return f"Error: workspace already exists: {workspace_name}"
            paths.root.mkdir(parents=True, exist_ok=False)
            paths.repo.mkdir()
            paths.artifacts.mkdir()
            source_provenance = _empty_workspace_provenance()
            materialized = False
            try:
                if source_path:
                    source = self._validate_source_path(source_path)
                    source_provenance = _source_provenance(source, expected_repo=repo_name, source_ref=ref)
                    _safe_copytree(source, paths.repo)
                    materialized = True
                _init_workspace_git(paths.repo)
                created_at = datetime.now(UTC)
                metadata = {
                    "workspace_id": workspace_name,
                    "repo": repo_name,
                    "ref": ref,
                    "workspace_dir": str(paths.root),
                    "repo_dir": str(paths.repo),
                    "artifacts_dir": str(paths.artifacts),
                    "created_at": created_at.isoformat(),
                    "expires_at": (created_at + timedelta(minutes=ttl)).isoformat(),
                    "ttl_minutes": ttl,
                    "network_policy": {
                        "allow_network": False,
                        "network_performed": False,
                        "note": "repo_workspace never clones, fetches, uploads, or performs network I/O.",
                    },
                    "execution_policy": {
                        "allows_execution": False,
                        "execution_performed": False,
                        "handoff_required_for_execution": "coding_sandbox",
                    },
                    "provenance": source_provenance,
                    "audit": {
                        "created_by_tool": "repo_workspace",
                        "materialization_method": "local_source_path_copy" if source_path else "empty_workspace",
                        "source_path_boundary_enforced": bool(source_path),
                        "allowed_source_roots": [str(root) for root in self.allowed_source_roots],
                        "network_performed": False,
                        "execution_performed": False,
                        "writes_require_confirmation": True,
                    },
                }
                _write_json(paths.metadata, metadata)
            except Exception:
                shutil.rmtree(paths.root, ignore_errors=True)
                raise
            return json.dumps({"status": "created", "workspace_id": workspace_name, "materialized": materialized, **_public_metadata(metadata)}, indent=2)
        except ValueError as exc:
            return f"Error: {exc}"

    def get_workspace_info(self, workspace_id: str) -> str:
        """Return provenance, lifecycle, and policy metadata for a workspace."""
        try:
            paths = self._existing_paths(workspace_id)
            metadata = self._load_metadata(paths)
            return json.dumps(_public_metadata(metadata), indent=2)
        except ValueError as exc:
            return f"Error: {exc}"

    def list_files(self, workspace_id: str, path: str = ".") -> str:
        """List files under the confined repository directory."""
        try:
            paths = self._existing_paths(workspace_id)
            target = _confined_path(paths.repo, path)
            if not target.exists():
                return f"Error: path does not exist: {path}"
            if target.is_file():
                return json.dumps([_relative_posix(target, paths.repo)], indent=2)
            files: list[str] = []
            for item in sorted(target.rglob("*")):
                if _is_internal_git_path(item, paths.repo):
                    continue
                marker = "/" if item.is_dir() else ""
                files.append(f"{_relative_posix(item, paths.repo)}{marker}")
            return json.dumps(files, indent=2)
        except ValueError as exc:
            return f"Error: {exc}"

    def read_file(self, workspace_id: str, path: str, offset: int = 0, limit: int | None = None) -> str:
        """Read a UTF-8 text file from the confined repository directory."""
        try:
            paths = self._existing_paths(workspace_id)
            target = _confined_path(paths.repo, path)
            if not target.is_file():
                return f"Error: not a file: {path}"
            if target.stat().st_size > self.max_file_bytes:
                return f"Error: file exceeds max_file_bytes ({self.max_file_bytes})."
            text = target.read_text(encoding="utf-8")
            if offset < 0:
                return "Error: offset must be non-negative."
            lines = text.splitlines(keepends=True)
            selected = lines[offset : offset + limit if limit is not None else None]
            return "".join(selected)
        except UnicodeDecodeError:
            return "Error: file is not valid UTF-8 text."
        except ValueError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"

    def write_file(self, workspace_id: str, path: str, content: str, confirm_write: bool = False) -> str:
        """Write a UTF-8 text file inside the workspace repo directory."""
        if not confirm_write:
            return _write_confirmation_error("write_file")
        try:
            paths = self._existing_paths(workspace_id)
            target = _confined_path(paths.repo, path)
            if _is_internal_git_path(target, paths.repo):
                return "Error: refusing to write internal .git paths."
            encoded = content.encode("utf-8")
            if len(encoded) > self.max_file_bytes:
                return f"Error: content exceeds max_file_bytes ({self.max_file_bytes})."
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {path}."
        except ValueError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"

    def apply_patch(self, workspace_id: str, patch: str, confirm_write: bool = False) -> str:
        """Apply a unified diff to the workspace using git apply without executing repo code."""
        if not confirm_write:
            return _write_confirmation_error("apply_patch")
        try:
            paths = self._existing_paths(workspace_id)
            result = _run_git(paths.repo, "apply", "--whitespace=nowarn", "-", input_text=patch)
            if result.returncode != 0:
                return _format_completed_process("git apply failed", result)
            return "Patch applied."
        except ValueError as exc:
            return f"Error: {exc}"

    def get_status(self, workspace_id: str) -> str:
        """Return git status porcelain for the workspace."""
        try:
            paths = self._existing_paths(workspace_id)
            result = _run_git(paths.repo, "status", "--short")
            if result.returncode != 0:
                return _format_completed_process("git status failed", result)
            return result.stdout
        except ValueError as exc:
            return f"Error: {exc}"

    def get_diff(self, workspace_id: str, path: str | None = None) -> str:
        """Return a unified diff for workspace changes without invoking external diff drivers."""
        try:
            paths = self._existing_paths(workspace_id)
            args = ["diff", "--no-ext-diff", "--"]
            if path:
                target = _confined_path(paths.repo, path)
                args.append(_relative_posix(target, paths.repo))
            result = _run_git(paths.repo, *args)
            if result.returncode not in (0, 1):
                return _format_completed_process("git diff failed", result)
            return result.stdout
        except ValueError as exc:
            return f"Error: {exc}"

    def export_patch(self, workspace_id: str, artifact_name: str = "changes.patch", confirm_write: bool = False) -> str:
        """Export current diff to an artifact file and return the artifact path."""
        if not confirm_write:
            return _write_confirmation_error("export_patch")
        try:
            paths = self._existing_paths(workspace_id)
            safe_name = _validate_artifact_name(artifact_name)
            diff = self.get_diff(workspace_id)
            artifact_path = paths.artifacts / safe_name
            artifact_path.write_text(diff, encoding="utf-8")
            return json.dumps({"artifact": str(artifact_path), "bytes": len(diff.encode("utf-8"))}, indent=2)
        except ValueError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"

    def handoff_to_coding_sandbox(
        self,
        workspace_id: str,
        command: str | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Produce a descriptor for an external coding sandbox; this tool does not execute it."""
        try:
            paths = self._existing_paths(workspace_id)
            metadata = self._load_metadata(paths)
            descriptor = {
                "type": "coding_sandbox_handoff",
                "workspace_id": workspace_id,
                "repo": metadata["repo"],
                "repo_dir": str(paths.repo),
                "artifacts_dir": str(paths.artifacts),
                "requested_command": command,
                "command": command,
                "timeout_seconds": timeout_seconds,
                "authorization": {
                    "status": "not_authorized_by_repo_workspace",
                    "note": "This descriptor is a request for an external execution substrate; it does not grant tool access or execution permission.",
                },
                "execution_policy": {
                    "requires_external_execution_substrate": "coding_sandbox",
                    "authorization_status": "not_authorized_by_repo_workspace",
                    "no_ambient_secrets": True,
                    "repo_workspace_executed_command": False,
                },
                "network_policy": metadata["network_policy"],
                "provenance": metadata["provenance"],
            }
            return json.dumps(descriptor, indent=2)
        except ValueError as exc:
            return f"Error: {exc}"

    def destroy_workspace(self, workspace_id: str, confirm_write: bool = False) -> str:
        """Delete a workspace directory after explicit confirmation."""
        if not confirm_write:
            return _write_confirmation_error("destroy_workspace")
        try:
            paths = self._existing_paths(workspace_id)
            shutil.rmtree(paths.root)
            return f"Destroyed workspace {workspace_id}."
        except ValueError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"

    def _paths(self, workspace_id: str) -> WorkspacePaths:
        safe_id = _validate_workspace_id(workspace_id)
        root = (self.workspace_root / safe_id).resolve()
        if not _is_relative_to(root, self.workspace_root):
            raise ValueError("workspace_id escapes workspace_root.")
        return WorkspacePaths(root=root, repo=root / "repo", artifacts=root / "artifacts", metadata=root / "workspace.json")

    def _existing_paths(self, workspace_id: str) -> WorkspacePaths:
        paths = self._paths(workspace_id)
        if not paths.metadata.is_file():
            raise ValueError(f"workspace not found: {workspace_id}")
        return paths

    def _load_metadata(self, paths: WorkspacePaths) -> dict[str, Any]:
        try:
            metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("workspace metadata is not valid JSON.") from exc
        if not isinstance(metadata, dict):
            raise ValueError("workspace metadata is invalid.")
        validation_error = _validate_workspace_metadata(metadata, paths.root, configured_source_roots=self.allowed_source_roots)
        if validation_error:
            raise ValueError(validation_error.removeprefix("Error: "))
        return metadata

    def _validate_source_path(self, source_path: str) -> Path:
        if not self.allowed_source_roots:
            raise ValueError("source_path requires allowed_source_roots to be configured.")
        source = Path(source_path).resolve()
        if not source.is_dir():
            raise ValueError("source_path must be an existing directory.")
        if not any(_is_relative_to(source, root) for root in self.allowed_source_roots):
            raise ValueError("source_path is outside allowed_source_roots.")
        return source

    def _check_repo_policy(self, repo: str) -> None:
        if self.allowed_repos and not any(_repo_matches(repo, pattern) for pattern in self.allowed_repos):
            raise ValueError(f"repository is not allowlisted: {repo}")
        if any(_repo_matches(repo, pattern) for pattern in self.denied_repos):
            raise ValueError(f"repository is explicitly denied: {repo}")

    def _validate_ttl(self, ttl_minutes: int | None) -> int:
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl_minutes
        if not isinstance(ttl, int) or ttl <= 0 or ttl > 24 * 60:
            raise ValueError("ttl_minutes must be an integer between 1 and 1440.")
        return ttl


def _normalize_string_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [item for item in value if isinstance(item, str) and item]


def _validate_repo_patterns(patterns: list[str], *, field: str) -> list[str]:
    validated: list[str] = []
    for pattern in patterns:
        if _ALLOWED_REPO_PATTERN.fullmatch(pattern) or _OWNER_WILDCARD_REPO_PATTERN.fullmatch(pattern):
            validated.append(pattern)
            continue
        if pattern in {"*", "*/*"}:
            raise ValueError(f"{field} must not contain broad wildcard pattern {pattern!r}; use owner/repo or owner/*.")
        if "*" in pattern:
            raise ValueError(f"{field} wildcard patterns must be owner-scoped like 'owner/*'.")
        expected = "owner/repo, owner/*" if field == "allowed_repos" else "owner/repo, owner/*"
        raise ValueError(f"{field} entries must be {expected}: {pattern!r}")
    return validated


def _repo_matches(repo: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(repo, pattern)


def _validate_repo_name(repo: str) -> str:
    if not isinstance(repo, str) or not _ALLOWED_REPO_PATTERN.fullmatch(repo):
        raise ValueError("repo must be in owner/name form.")
    return repo


def _validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", workspace_id):
        raise ValueError("workspace_id must be 1-80 chars of letters, numbers, dot, underscore, or hyphen.")
    return workspace_id


def _validate_artifact_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name):
        raise ValueError("artifact_name must be a simple filename.")
    return name


def _confined_path(root: Path, user_path: str) -> Path:
    candidate = (root / user_path).resolve()
    if not _is_relative_to(candidate, root.resolve()):
        raise ValueError("path escapes workspace repo directory.")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_internal_git_path(path: Path, repo: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return False
    return bool(rel.parts and rel.parts[0] == ".git")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _init_workspace_git(repo_dir: Path) -> None:
    if (repo_dir / ".git").exists():
        return
    result = _run_git(repo_dir, "init")
    if result.returncode != 0:
        raise ValueError(_format_completed_process("git init failed", result))


def _empty_workspace_provenance() -> dict[str, Any]:
    return {
        "source_path": None,
        "source_type": "empty_workspace",
        "repo_identity": None,
        "source_ref": None,
        "source_head_sha": None,
        "origin_url": None,
    }


def _source_provenance(source: Path, *, expected_repo: str, source_ref: str | None) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "source_path": str(source),
        "source_type": "local_copy",
        "repo_identity": None,
        "source_ref": source_ref,
        "source_head_sha": None,
        "origin_url": None,
    }
    git_dir = source / ".git"
    if not git_dir.exists():
        return provenance
    if not git_dir.is_dir():
        raise ValueError("source_path .git directory is not a valid git checkout.")
    _verify_git_checkout_root(source)
    head_sha = _git_stdout(source, "rev-parse", "--verify", "HEAD^{commit}")
    if not head_sha:
        raise ValueError("source_path .git checkout has no verifiable HEAD commit.")
    origin_url_raw = _git_stdout(source, "remote", "get-url", "origin")
    origin_url = _redact_url(origin_url_raw) if origin_url_raw else None
    repo_identity = _repo_identity_from_origin(origin_url_raw)
    if repo_identity is None:
        raise ValueError("source_path git origin does not match requested repo.")
    if repo_identity.lower() != expected_repo.lower():
        raise ValueError(f"source_path git origin does not match requested repo: {repo_identity}")
    provenance.update(
        {
            "source_type": "local_git_checkout",
            "repo_identity": repo_identity,
            "source_head_sha": head_sha,
            "origin_url": origin_url,
        }
    )
    return provenance


def _verify_git_checkout_root(source: Path) -> None:
    git_dir = source / ".git"
    if not git_dir.is_dir():
        raise ValueError("source_path .git directory is not a valid git checkout.")
    common_dir = _git_stdout(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common_dir:
        raise ValueError("source_path .git checkout could not be verified.")
    try:
        resolved_common = Path(common_dir).resolve()
    except OSError as exc:
        raise ValueError("source_path .git checkout could not be verified.") from exc
    if resolved_common != git_dir.resolve():
        raise ValueError("source_path must be the root of a non-linked local git checkout.")


def _git_stdout(repo_dir: Path, *args: str) -> str | None:
    result = _run_git(repo_dir, *args)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _redact_url(url: str) -> str:
    if not url:
        return url
    return re.sub(r"(https?://)([^/@]+@)", r"\1", url)


def _repo_identity_from_origin(origin_url: str | None) -> str | None:
    if not origin_url:
        return None
    patterns = [
        r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"^https?://(?:[^/@]+@)?github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, origin_url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def _safe_subprocess_env() -> dict[str, str]:
    deny_substrings = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY")
    deny_exact = {"GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in deny_exact and not any(fragment in key.upper() for fragment in deny_substrings)
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
    )
    return env


def _run_git(repo_dir: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_SAFE_GIT_CONFIG_ARGS, *args],
        cwd=repo_dir,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=15,
        env=_safe_subprocess_env(),
        check=False,
    )


def _safe_copytree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        try:
            st = path.lstat()
        except OSError as exc:
            raise ValueError(f"source_path contains an unreadable path: {path}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise ValueError("source_path contains symlinks; refusing to materialize ambiguous paths.")
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise ValueError("source_path contains special files; refusing to materialize ambiguous paths.")
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if rel.parts and rel.parts[0] == ".git":
            continue
        target = destination / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    expires_raw = metadata.get("expires_at")
    expired = False
    if isinstance(expires_raw, str):
        try:
            expired = datetime.fromisoformat(expires_raw) <= now
        except ValueError:
            expired = False
    repo_dir = Path(str(metadata.get("repo_dir", "")))
    dirty = False
    if repo_dir.exists():
        status = _run_git(repo_dir, "status", "--short")
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    allowed_keys = {
        "workspace_id",
        "repo",
        "ref",
        "workspace_dir",
        "repo_dir",
        "artifacts_dir",
        "created_at",
        "expires_at",
        "ttl_minutes",
        "network_policy",
        "execution_policy",
        "provenance",
        "audit",
        "exists",
        "expired",
        "dirty",
    }
    return {key: metadata[key] for key in allowed_keys if key in metadata}


def _validate_workspace_metadata(
    metadata: dict[str, Any],
    workspace_dir: Path,
    *,
    configured_source_roots: list[Path] | None = None,
) -> str | None:
    repo_dir = Path(str(metadata.get("repo_dir", ""))).resolve()
    expected_repo_dir = (workspace_dir / "repo").resolve()
    if repo_dir != expected_repo_dir:
        return "Error: workspace metadata repo_dir does not match expected confined path."

    audit = metadata.get("audit")
    if not isinstance(audit, dict):
        return "Error: workspace metadata is missing required audit fields."
    if audit.get("created_by_tool") != "repo_workspace":
        return "Error: workspace metadata audit has invalid created_by_tool."
    if audit.get("network_performed") is not False or audit.get("execution_performed") is not False:
        return "Error: workspace metadata audit violates repo_workspace non-execution policy."
    if audit.get("writes_require_confirmation") is not True:
        return "Error: workspace metadata audit is missing write confirmation policy."

    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        return "Error: workspace metadata is missing required provenance."
    source_type = provenance.get("source_type")
    if source_type not in {"empty_workspace", "local_copy", "local_git_checkout"}:
        return "Error: workspace metadata provenance has invalid source_type."
    if source_type == "empty_workspace":
        source_path = provenance.get("source_path")
        if source_path not in {None, ""}:
            return "Error: workspace metadata empty_workspace provenance must not include source_path."
    else:
        if audit.get("source_path_boundary_enforced") is not True:
            return "Error: workspace metadata audit must enforce source_path boundary for local sources."
        source_path = provenance.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            return "Error: workspace metadata provenance is missing source_path."
        try:
            resolved_source = Path(source_path).resolve()
        except OSError:
            return "Error: workspace metadata provenance has invalid source_path."
        recorded_roots = _recorded_allowed_source_roots(audit)
        if not recorded_roots:
            return "Error: workspace metadata audit is missing allowed_source_roots for local source_path."
        if not any(_is_relative_to(resolved_source, root) for root in recorded_roots):
            return "Error: workspace metadata provenance source_path is outside allowed_source_roots."
        configured_roots = [root.resolve() for root in (configured_source_roots or [])]
        if configured_roots and not any(_is_relative_to(resolved_source, root) for root in configured_roots):
            return "Error: workspace metadata provenance source_path is outside configured allowed_source_roots."
    return None


def _recorded_allowed_source_roots(audit: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    recorded_roots = audit.get("allowed_source_roots")
    if isinstance(recorded_roots, list):
        for item in recorded_roots:
            if not isinstance(item, str) or not item:
                continue
            try:
                root = Path(item).resolve()
            except OSError:
                continue
            if root not in roots:
                roots.append(root)
    return roots


def _write_confirmation_error(action: str) -> str:
    return f"Error: {action} requires confirm_write=true because it modifies workspace state."


def _format_completed_process(label: str, result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return _truncate_output(f"Error: {label} (exit {result.returncode})\n{output}".strip())


def _truncate_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


repo_workspace_tools = RepoWorkspaceTools()