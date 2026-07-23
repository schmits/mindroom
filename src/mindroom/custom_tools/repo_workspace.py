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
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

from mindroom.tools.path_safety import resolve_base_dir_path

_DEFAULT_ALLOWED_REPOS = ("schmits/repo-sandbox-fixture",)
_DEFAULT_DENIED_REPOS = ("schmits/prod", "schmits/production", "schmits/secrets", "schmits/security")
_MAX_COMMAND_OUTPUT_BYTES = 60_000
_DEFAULT_MAX_TTL_MINUTES = 120
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")


class RepoWorkspaceTools(Toolkit):
    """Repository-scoped local workspace substrate.

    ``repo_workspace`` deliberately separates local file/diff handling from
    remote GitHub writes and command execution:

    * no arbitrary shell execution is exposed;
    * no clone/fetch/network operation is performed by the MVP;
    * no ambient secrets are passed to subprocesses;
    * all file operations are confined to a workspace ``repo/`` directory;
    * mutating operations require ``confirm_write=True``;
    * execution requests are represented as handoff descriptors for a separate
      coding sandbox.
    """

    def __init__(
        self,
        workspace_root: str | None = None,
        allowed_repos: list[str] | str | None = None,
        denied_repos: list[str] | str | None = None,
        allowed_source_roots: list[str] | str | None = None,
        max_ttl_minutes: int = _DEFAULT_MAX_TTL_MINUTES,
        allow_network: bool = False,
        default_repo: str = "schmits/repo-sandbox-fixture",
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else (Path.cwd() / "repo_workspace").resolve()
        self.allowed_repos = _normalize_repo_patterns(allowed_repos, fallback=_DEFAULT_ALLOWED_REPOS, field_name="allowed_repos")
        self.denied_repos = _normalize_repo_patterns(denied_repos, fallback=_DEFAULT_DENIED_REPOS, field_name="denied_repos")
        self.allowed_source_roots = [
            Path(item).resolve() for item in _normalize_string_list(allowed_source_roots)
        ]
        self.max_ttl_minutes = max_ttl_minutes
        self.allow_network = allow_network
        self.default_repo = default_repo
        super().__init__(
            name="repo_workspace",
            tools=[
                self.create_workspace,
                self.get_workspace_info,
                self.list_workspaces,
                self.list_files,
                self.read_file,
                self.write_file,
                self.delete_file,
                self.apply_patch,
                self.get_status,
                self.get_diff,
                self.export_patch,
                self.handoff_to_coding_sandbox,
                self.destroy_workspace,
            ],
        )

    def create_workspace(
        self,
        repo: str | None = None,
        ref: str | None = None,
        source_path: str | None = None,
        workspace_id: str | None = None,
        ttl_minutes: int | None = None,
        allow_network: bool | None = None,
        confirm_write: bool = False,
    ) -> str:
        """Create an ephemeral repo-scoped workspace.

        Args:
            repo: Repository in owner/name form. Defaults to configured ``default_repo``.
            ref: Optional provenance ref/SHA/branch label. It is recorded, not checked out.
            source_path: Optional local directory to copy into the workspace repo directory.
                If provided, it must be inside one of ``allowed_source_roots``.
            workspace_id: Optional caller-chosen id. If omitted, a random id is generated.
            ttl_minutes: Workspace retention hint, capped by ``max_ttl_minutes``.
            allow_network: Explicit network policy. The MVP records this value but never
                performs network I/O.
            confirm_write: Required because this creates local files/directories.
        """
        if not confirm_write:
            return _write_confirmation_error("create_workspace")
        try:
            repo_name = self._validate_repo(repo)
            workspace_id = self._new_workspace_id(repo_name, workspace_id)
            ttl = self._validate_ttl(ttl_minutes)
        except ValueError as exc:
            return f"Error: {exc}"

        effective_network = self.allow_network if allow_network is None else bool(allow_network)
        workspace_dir = self._workspace_dir(workspace_id)
        if workspace_dir.exists():
            return f"Error: workspace already exists: {workspace_id}"
        repo_dir = workspace_dir / "repo"
        artifacts_dir = workspace_dir / "artifacts"
        try:
            repo_dir.mkdir(parents=True, exist_ok=False)
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            if source_path:
                source = self._validate_source_path(source_path)
                _copy_source_tree(source, repo_dir)
        except ValueError as exc:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return f"Error: {exc}"
        except OSError as exc:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return f"Error creating workspace: {exc}"

        created_at = datetime.now(UTC)
        metadata = {
            "workspace_id": workspace_id,
            "repo": repo_name,
            "ref": ref,
            "workspace_dir": str(workspace_dir),
            "repo_dir": str(repo_dir),
            "artifacts_dir": str(artifacts_dir),
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=ttl)).isoformat(),
            "ttl_minutes": ttl,
            "network_policy": {
                "allow_network": effective_network,
                "network_performed": False,
                "note": "Network/clone/fetch is not implemented by repo_workspace MVP.",
            },
            "execution_policy": {
                "allow_arbitrary_execution": False,
                "execution_performed": False,
                "handoff_required": "coding_sandbox",
            },
            "provenance": {
                "source_path": str(Path(source_path).resolve()) if source_path else None,
                "source_type": "local_copy" if source_path else "empty_workspace",
            },
        }
        self._write_metadata(workspace_dir, metadata)
        return json.dumps({"status": "created", "workspace": _public_metadata(metadata)}, indent=2, sort_keys=True)

    def get_workspace_info(self, workspace_id: str) -> str:
        """Return provenance, lifecycle, and policy metadata for a workspace."""
        loaded = self._load_workspace(workspace_id)
        if isinstance(loaded, str):
            return loaded
        metadata, workspace_dir = loaded
        metadata = dict(metadata)
        metadata["exists"] = workspace_dir.exists()
        metadata["expired"] = _is_expired(metadata)
        metadata["dirty"] = _workspace_has_changes(Path(metadata["repo_dir"]))
        return json.dumps(_public_metadata(metadata), indent=2, sort_keys=True)

    def list_workspaces(self, include_expired: bool = True, limit: int = 100) -> str:
        """List known workspaces under the configured workspace root."""
        if limit <= 0:
            return "Error: limit must be positive."
        if not self.workspace_root.exists():
            return "No workspaces found."
        items: list[dict[str, Any]] = []
        for path in sorted(self.workspace_root.iterdir()):
            if not path.is_dir():
                continue
            metadata_path = path / "workspace.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            expired = _is_expired(metadata)
            if expired and not include_expired:
                continue
            metadata = _public_metadata(metadata)
            metadata["expired"] = expired
            items.append(metadata)
            if len(items) >= limit:
                break
        if not items:
            return "No workspaces found."
        return json.dumps(items, indent=2, sort_keys=True)

    def list_files(self, workspace_id: str, pattern: str = "**/*", limit: int = 500) -> str:
        """List files in a workspace repo directory."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        if limit <= 0:
            return "Error: limit must be positive."
        matches: list[str] = []
        for path in repo_dir.rglob("*"):
            rel_path = _safe_relative_path(path, repo_dir)
            if rel_path is None or ".git" in rel_path.parts:
                continue
            rel = rel_path.as_posix() + ("/" if path.is_dir() else "")
            if fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
            if len(matches) >= limit:
                break
        if not matches:
            return "No files found."
        suffix = f"\n[limited to {limit} entries]" if len(matches) >= limit else ""
        return "\n".join(matches) + suffix

    def read_file(self, workspace_id: str, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file from a workspace repo with line numbers."""
        resolved = self._resolve_workspace_file(workspace_id, path, must_exist=True)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"Error: path is not a file: {path}"
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return "Error: file is not valid UTF-8 text."
        except OSError as exc:
            return f"Error reading file: {exc}"
        start = max((offset or 1) - 1, 0)
        end = start + limit if limit is not None else len(lines)
        selected = lines[start:end]
        return "\n".join(f"{index + 1:>4}| {line}" for index, line in enumerate(selected, start=start))

    def write_file(self, workspace_id: str, path: str, content: str, confirm_write: bool = False) -> str:
        """Write a UTF-8 text file inside a workspace repo. Requires confirmation."""
        if not confirm_write:
            return _write_confirmation_error("write_file")
        resolved = self._resolve_workspace_file(workspace_id, path, must_exist=False)
        if isinstance(resolved, str):
            return resolved
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"Error writing file: {exc}"
        return f"Wrote {path} ({len(content.encode('utf-8'))} bytes)."

    def delete_file(self, workspace_id: str, path: str, confirm_write: bool = False) -> str:
        """Delete a file inside a workspace repo. Requires confirmation."""
        if not confirm_write:
            return _write_confirmation_error("delete_file")
        resolved = self._resolve_workspace_file(workspace_id, path, must_exist=True)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"Error: path is not a file: {path}"
        try:
            resolved.unlink()
        except OSError as exc:
            return f"Error deleting file: {exc}"
        return f"Deleted {path}."

    def apply_patch(self, workspace_id: str, patch: str, confirm_write: bool = False) -> str:
        """Apply a unified patch inside the workspace repo. Requires confirmation.

        This uses ``git apply`` with a sanitized environment and no shell. It does
        not fetch, clone, install dependencies, or execute project scripts.
        """
        if not confirm_write:
            return _write_confirmation_error("apply_patch")
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        try:
            result = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                input=patch,
                cwd=repo_dir,
                env=_safe_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return "Error: git executable not found."
        except subprocess.TimeoutExpired:
            return "Error: git apply timed out."
        if result.returncode != 0:
            return _format_completed_process("git apply failed", result)
        return "Patch applied."

    def get_status(self, workspace_id: str) -> str:
        """Return workspace status and compact diff summary."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        if (repo_dir / ".git").is_dir():
            status_result = _run_git(["status", "--short", "--branch"], cwd=repo_dir)
            diff_result = _run_git(["diff", "--stat", "--"], cwd=repo_dir)
            parts = ["# git status", status_result.stdout or status_result.stderr]
            if diff_result.stdout:
                parts.extend(["\n# git diff --stat", diff_result.stdout])
            return _truncate_output("\n".join(parts).strip())
        files = [path for path in repo_dir.rglob("*") if path.is_file()]
        return f"Workspace has no .git directory. Files: {len(files)}. Diff unavailable until materialized from a git checkout or patch."

    def get_diff(self, workspace_id: str, path: str | None = None) -> str:
        """Return a unified git diff for a workspace checkout."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        if not (repo_dir / ".git").is_dir():
            return "Error: diff requires a git checkout with a .git directory."
        args = ["diff", "--"]
        if path:
            resolved = self._resolve_workspace_file(workspace_id, path, must_exist=False)
            if isinstance(resolved, str):
                return resolved
            args.append(path)
        result = _run_git(args, cwd=repo_dir)
        if result.returncode != 0:
            return _format_completed_process("git diff failed", result)
        return _truncate_output(result.stdout or "No diff.")

    def export_patch(self, workspace_id: str, artifact_name: str = "workspace.patch", confirm_write: bool = False) -> str:
        """Export the current git diff as a patch artifact. Requires confirmation."""
        if not confirm_write:
            return _write_confirmation_error("export_patch")
        loaded = self._load_workspace(workspace_id)
        if isinstance(loaded, str):
            return loaded
        metadata, workspace_dir = loaded
        repo_dir = Path(metadata["repo_dir"])
        if not (repo_dir / ".git").is_dir():
            return "Error: export_patch requires a git checkout with a .git directory."
        if "/" in artifact_name or "\\" in artifact_name or artifact_name in {"", ".", ".."}:
            return "Error: artifact_name must be a simple filename."
        result = _run_git(["diff", "--binary", "--"], cwd=repo_dir)
        if result.returncode != 0:
            return _format_completed_process("git diff failed", result)
        artifact_path = resolve_base_dir_path(workspace_dir / "artifacts", artifact_name)
        try:
            artifact_path.write_text(result.stdout, encoding="utf-8")
        except OSError as exc:
            return f"Error writing artifact: {exc}"
        return json.dumps({"artifact": str(artifact_path), "bytes": len(result.stdout.encode("utf-8"))}, indent=2, sort_keys=True)

    def handoff_to_coding_sandbox(
        self,
        workspace_id: str,
        command: str | None = None,
        allow_network: bool = False,
        allow_dependency_install: bool = False,
        timeout_seconds: int = 300,
    ) -> str:
        """Return a controlled execution handoff descriptor for coding_sandbox.

        No command is executed here. The descriptor is intended for an execution
        substrate that enforces its own policy, timeout, output capture, and
        secret isolation.
        """
        loaded = self._load_workspace(workspace_id)
        if isinstance(loaded, str):
            return loaded
        metadata, _workspace_dir = loaded
        if timeout_seconds <= 0 or timeout_seconds > 3600:
            return "Error: timeout_seconds must be between 1 and 3600."
        descriptor = {
            "type": "coding_sandbox_handoff",
            "workspace_id": workspace_id,
            "repo": metadata.get("repo"),
            "ref": metadata.get("ref"),
            "repo_dir": metadata.get("repo_dir"),
            "command": command,
            "execution_policy": {
                "allow_arbitrary_execution": False,
                "requires_external_execution_substrate": "coding_sandbox",
                "allow_network": allow_network,
                "allow_dependency_install": allow_dependency_install,
                "timeout_seconds": timeout_seconds,
                "capture_stdout": True,
                "capture_stderr": True,
                "capture_exit_code": True,
                "no_ambient_secrets": True,
            },
        }
        return json.dumps(descriptor, indent=2, sort_keys=True)

    def destroy_workspace(self, workspace_id: str, confirm_write: bool = False) -> str:
        """Destroy a workspace directory. Requires confirmation."""
        if not confirm_write:
            return _write_confirmation_error("destroy_workspace")
        loaded = self._load_workspace(workspace_id)
        if isinstance(loaded, str):
            return loaded
        _metadata, workspace_dir = loaded
        try:
            shutil.rmtree(workspace_dir)
        except OSError as exc:
            return f"Error destroying workspace: {exc}"
        return f"Destroyed workspace {workspace_id}."

    def _validate_repo(self, repo: str | None) -> str:
        repo_name = (repo or self.default_repo).strip()
        if not _GITHUB_REPO_RE.fullmatch(repo_name):
            raise ValueError("repo must be in owner/name form with safe GitHub characters.")
        if any(fnmatch.fnmatch(repo_name, pattern) for pattern in self.denied_repos):
            raise ValueError(f"repo is explicitly denied: {repo_name}")
        if not any(fnmatch.fnmatch(repo_name, pattern) for pattern in self.allowed_repos):
            raise ValueError(f"repo is not allowlisted: {repo_name}")
        return repo_name

    def _validate_ttl(self, ttl_minutes: int | None) -> int:
        ttl = self.max_ttl_minutes if ttl_minutes is None else ttl_minutes
        if ttl <= 0:
            raise ValueError("ttl_minutes must be positive.")
        if ttl > self.max_ttl_minutes:
            raise ValueError(f"ttl_minutes exceeds max_ttl_minutes ({self.max_ttl_minutes}).")
        return ttl

    def _validate_source_path(self, source_path: str) -> Path:
        if not self.allowed_source_roots:
            raise ValueError("source_path requires allowed_source_roots to be configured.")
        source = Path(source_path).resolve()
        if not source.is_dir():
            raise ValueError("source_path must be an existing directory.")
        if not any(_is_relative_to(source, root) for root in self.allowed_source_roots):
            raise ValueError("source_path is outside allowed_source_roots.")
        return source

    def _new_workspace_id(self, repo: str, workspace_id: str | None) -> str:
        if workspace_id is None:
            owner, name = repo.split("/", 1)
            workspace_id = f"{owner}-{name}-{uuid.uuid4().hex[:12]}".lower()
        if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
            raise ValueError("workspace_id contains unsupported characters.")
        return workspace_id

    def _workspace_dir(self, workspace_id: str) -> Path:
        if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
            raise ValueError("workspace_id contains unsupported characters.")
        return resolve_base_dir_path(self.workspace_root, workspace_id)

    def _load_workspace(self, workspace_id: str) -> tuple[dict[str, Any], Path] | str:
        try:
            workspace_dir = self._workspace_dir(workspace_id)
        except ValueError as exc:
            return f"Error: {exc}"
        metadata_path = workspace_dir / "workspace.json"
        if not metadata_path.is_file():
            return f"Error: workspace not found: {workspace_id}"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Error reading workspace metadata: {exc}"
        repo_dir = Path(str(metadata.get("repo_dir", ""))).resolve()
        expected_repo_dir = (workspace_dir / "repo").resolve()
        if repo_dir != expected_repo_dir:
            return "Error: workspace metadata repo_dir does not match expected confined path."
        return metadata, workspace_dir

    def _repo_dir_for_workspace(self, workspace_id: str) -> Path | str:
        loaded = self._load_workspace(workspace_id)
        if isinstance(loaded, str):
            return loaded
        metadata, _workspace_dir = loaded
        repo_dir = Path(metadata["repo_dir"]).resolve()
        if not repo_dir.is_dir():
            return f"Error: workspace repo directory missing: {workspace_id}"
        return repo_dir

    def _resolve_workspace_file(self, workspace_id: str, path: str, must_exist: bool) -> Path | str:
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        try:
            resolved = resolve_base_dir_path(repo_dir, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if ".git" in Path(path).parts:
            return "Error: direct access to .git internals is not allowed."
        if must_exist and not resolved.exists():
            return f"Error: path does not exist: {path}"
        return resolved

    def _write_metadata(self, workspace_dir: Path, metadata: dict[str, Any]) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "workspace.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalize_string_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,\n]", value) if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_repo_patterns(value: list[str] | str | None, *, fallback: tuple[str, ...], field_name: str) -> list[str]:
    patterns = _normalize_string_list(value) or list(fallback)
    for pattern in patterns:
        if ".." in pattern or pattern.startswith("/") or "\\" in pattern:
            raise ValueError(f"{field_name} contains an unsafe repository pattern: {pattern!r}")
    return patterns


def _safe_subprocess_env() -> dict[str, str]:
    """Return a minimal environment without ambient credentials."""
    allowed = {"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "WINDIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_safe_subprocess_env(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _copy_source_tree(source: Path, destination: Path) -> None:
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            raise ValueError("source_path contains symlinks; refusing to materialize ambiguous paths.")
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False, ignore_dangling_symlinks=False)
        elif child.is_file():
            shutil.copy2(child, target)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _safe_relative_path(path: Path, base: Path) -> Path | None:
    try:
        return path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return None


def _is_expired(metadata: dict[str, Any]) -> bool:
    expires_at = metadata.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(UTC)
    except ValueError:
        return False


def _workspace_has_changes(repo_dir: Path) -> bool:
    if not (repo_dir / ".git").is_dir():
        return any(repo_dir.rglob("*"))
    result = _run_git(["status", "--porcelain", "--"], cwd=repo_dir)
    return bool(result.stdout.strip()) if result.returncode == 0 else True


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
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
        "exists",
        "expired",
        "dirty",
    }
    return {key: metadata[key] for key in allowed_keys if key in metadata}


def _write_confirmation_error(action: str) -> str:
    return f"Error: {action} requires confirm_write=true because it modifies workspace state."


def _format_completed_process(label: str, result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return _truncate_output(f"Error: {label} (exit {result.returncode})\n{output}".strip())


def _truncate_output(output: str, limit: int = _MAX_COMMAND_OUTPUT_BYTES) -> str:
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return output
    truncated = encoded[:limit].decode("utf-8", errors="replace")
    return f"{truncated}\n[truncated to {limit} bytes]"