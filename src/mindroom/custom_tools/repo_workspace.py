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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    remote GitHub writes and command execution. Workspaces are copied from
    already-present source directories and all mutation APIs require explicit
    confirmation.
    """

    def __init__(
        self,
        workspace_root: str | None = None,
        *,
        allowed_repos: list[str] | tuple[str, ...] | None = None,
        denied_repos: list[str] | tuple[str, ...] | None = None,
        allowed_source_roots: list[str] | tuple[str, ...] | None = None,
        default_repo: str = "schmits/repo-sandbox-fixture",
        max_ttl_minutes: int = _DEFAULT_MAX_TTL_MINUTES,
        **kwargs: Any,
    ) -> None:
        super().__init__(name="repo_workspace", tools=[])
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise ValueError(f"Unsupported repo_workspace option(s): {unexpected}")
        root = workspace_root or str(Path.cwd() / "repo_workspaces")
        self.workspace_root = resolve_base_dir_path(root, option_name="workspace_root")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.allowed_repos = _normalize_patterns(allowed_repos or _DEFAULT_ALLOWED_REPOS, option_name="allowed_repos")
        self.denied_repos = _normalize_patterns(denied_repos or _DEFAULT_DENIED_REPOS, option_name="denied_repos")
        self.allowed_source_roots = tuple(
            resolve_base_dir_path(path, option_name="allowed_source_roots") for path in (allowed_source_roots or [])
        )
        self.default_repo = _validate_repo(default_repo)
        self.max_ttl_minutes = max(1, int(max_ttl_minutes))
        self.register(self.create_workspace)
        self.register(self.list_files)
        self.register(self.read_file)
        self.register(self.grep)
        self.register(self.write_file)
        self.register(self.edit_file)
        self.register(self.get_status)
        self.register(self.export_patch)
        self.register(self.get_workspace_info)
        self.register(self.create_handoff)
        self.register(self.cleanup_workspace)

    def create_workspace(
        self,
        repo: str | None = None,
        workspace_id: str | None = None,
        source_path: str | None = None,
        ref: str | None = None,
        confirm_write: bool = False,
    ) -> str:
        """Create an isolated workspace from an already-present local source tree.

        ``source_path`` is optional and must resolve beneath one of
        ``allowed_source_roots``. No network clone/fetch/pull is performed.
        """
        if not confirm_write:
            return _write_confirmation_error("create_workspace")
        repo_name = self._authorize_repo(repo or self.default_repo)
        if isinstance(repo_name, str) and repo_name.startswith("Error:"):
            return repo_name
        workspace_id = workspace_id or _new_workspace_id(repo_name)
        try:
            workspace_id = _validate_workspace_id(workspace_id)
        except ValueError as exc:
            return f"Error: {exc}"
        workspace_dir = self._workspace_dir(workspace_id)
        if workspace_dir.exists():
            return f"Error: workspace already exists: {workspace_id}"
        repo_dir = workspace_dir / "repo"
        artifacts_dir = workspace_dir / "artifacts"
        source_provenance = _empty_workspace_provenance()
        try:
            repo_dir.mkdir(parents=True, exist_ok=False)
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            if source_path:
                source = self._validate_source_path(source_path)
                source_provenance = _source_provenance(source, expected_repo=repo_name, source_ref=ref)
                _copy_source_tree(source, repo_dir)
        except ValueError as exc:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return f"Error: {exc}"
        except OSError as exc:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return f"Error creating workspace: {exc}"
        now = datetime.now(UTC)
        metadata = {
            "workspace_id": workspace_id,
            "repo": repo_name,
            "ref": ref,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=self.max_ttl_minutes)).isoformat(),
            "workspace_dir": str(workspace_dir),
            "repo_dir": str(repo_dir),
            "artifacts_dir": str(artifacts_dir),
            "network_policy": {
                "network_clone_allowed": False,
                "git_fetch_allowed": False,
                "github_write_allowed": False,
                "network_performed": False,
            },
            "execution_policy": {
                "allow_arbitrary_execution": False,
                "allow_package_install": False,
                "execution_performed": False,
                "handoff_required": "coding_sandbox",
            },
            "provenance": source_provenance,
            "audit": {
                "created_by_tool": "repo_workspace",
                "materialization_method": "local_source_path_copy" if source_path else "empty_workspace",
                "source_path_boundary_enforced": bool(source_path),
                "allowed_source_roots": [str(root) for root in self.allowed_source_roots],
                "writes_require_confirmation": True,
                "network_performed": False,
                "execution_performed": False,
            },
        }
        self._write_metadata(workspace_dir, metadata)
        return json.dumps({"workspace": _public_metadata(metadata)}, sort_keys=True)

    def list_files(self, workspace_id: str, pattern: str = "**/*", limit: int = 200) -> str:
        """List files in a workspace repo without exposing .git internals."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        limit = max(1, min(int(limit), 1000))
        matches: list[str] = []
        for path in sorted(repo_dir.rglob("*")):
            if len(matches) >= limit:
                break
            if not path.is_file():
                continue
            rel = _relative_repo_path(repo_dir, path)
            if _is_hidden_git_path(rel):
                continue
            if fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
        return "\n".join(matches) if matches else "No files matched."

    def read_file(self, workspace_id: str, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        """Read a text file with line numbers from a confined workspace repo."""
        resolved = self._resolve_repo_file(workspace_id, path, must_exist=True)
        if isinstance(resolved, str):
            return resolved
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            return f"Error reading file: {exc}"
        if b"\0" in data:
            return "Error: refusing to read binary file."
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start_line = max(1, int(start_line))
        max_lines = max(1, min(int(max_lines), 1000))
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return "\n".join(f"{idx}| {line}" for idx, line in enumerate(selected, start=start_line))

    def grep(self, workspace_id: str, pattern: str, glob_pattern: str = "**/*", limit: int = 100) -> str:
        """Search UTF-8-ish text files in a workspace repo using Python regex."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"
        results: list[str] = []
        limit = max(1, min(int(limit), 500))
        for path in sorted(repo_dir.rglob("*")):
            if len(results) >= limit:
                break
            if not path.is_file():
                continue
            rel = _relative_repo_path(repo_dir, path)
            if _is_hidden_git_path(rel) or not fnmatch.fnmatch(rel, glob_pattern):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data:
                continue
            for line_no, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{rel}:{line_no}: {line}")
                    if len(results) >= limit:
                        break
        return "\n".join(results) if results else "No matches found."

    def write_file(self, workspace_id: str, path: str, content: str, confirm_write: bool = False) -> str:
        """Write a UTF-8 text file in the confined workspace repo."""
        if not confirm_write:
            return _write_confirmation_error("write_file")
        resolved = self._resolve_repo_file(workspace_id, path, must_exist=False)
        if isinstance(resolved, str):
            return resolved
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"Error writing file: {exc}"
        return f"Wrote {path}"

    def edit_file(
        self,
        workspace_id: str,
        path: str,
        old_text: str,
        new_text: str,
        confirm_write: bool = False,
    ) -> str:
        """Replace exactly one text occurrence in a workspace file."""
        if not confirm_write:
            return _write_confirmation_error("edit_file")
        resolved = self._resolve_repo_file(workspace_id, path, must_exist=True)
        if isinstance(resolved, str):
            return resolved
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "Error: refusing to edit non-UTF-8 file."
        except OSError as exc:
            return f"Error reading file: {exc}"
        count = text.count(old_text)
        if count != 1:
            return f"Error: old_text matched {count} occurrences; expected exactly 1."
        try:
            resolved.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        except OSError as exc:
            return f"Error writing file: {exc}"
        return f"Edited {path}"

    def get_status(self, workspace_id: str) -> str:
        """Return safe status information for a workspace repo."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        if not (repo_dir / ".git").is_dir():
            return "No .git directory copied into this workspace; use list_files/read_file/export_patch for file state."
        status = _run_git(["status", "--short", "--branch"], cwd=repo_dir)
        diff_stat = _run_git(["diff", "--stat", "--no-ext-diff"], cwd=repo_dir)
        return "\n".join(
            [
                "# git status --short --branch",
                _bounded_output(status.stdout, status.stderr),
                "# git diff --stat --no-ext-diff",
                _bounded_output(diff_stat.stdout, diff_stat.stderr) or "(no unstaged diff)",
            ]
        )

    def export_patch(self, workspace_id: str, patch_name: str | None = None, include_untracked: bool = True) -> str:
        """Export a workspace diff artifact and return its confined artifact path."""
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        workspace = self._workspace_tuple(workspace_id)
        if isinstance(workspace, str):
            return workspace
        metadata, workspace_dir = workspace
        artifacts_dir = Path(metadata["artifacts_dir"]).resolve()
        if artifacts_dir != (workspace_dir / "artifacts").resolve():
            return "Error: workspace metadata artifacts_dir does not match expected confined path."
        patch_name = patch_name or f"{workspace_id}.patch"
        if "/" in patch_name or "\\" in patch_name or patch_name in {"", ".", ".."}:
            return "Error: patch_name must be a simple filename."
        patch_path = (artifacts_dir / patch_name).resolve()
        if not _is_relative_to(patch_path, artifacts_dir):
            return "Error: patch path escapes artifacts directory."
        patch_parts: list[str] = []
        if (repo_dir / ".git").is_dir():
            diff = _run_git(["diff", "--no-ext-diff", "--binary"], cwd=repo_dir)
            patch_parts.append(diff.stdout)
            if include_untracked:
                untracked = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=repo_dir)
                for rel in untracked.stdout.splitlines():
                    if _is_hidden_git_path(rel):
                        continue
                    patch_parts.append(_new_file_patch(repo_dir, rel))
        else:
            patch_parts.append(_tree_snapshot_patch(repo_dir))
        try:
            patch_path.write_text("\n".join(part for part in patch_parts if part), encoding="utf-8")
        except OSError as exc:
            return f"Error writing patch artifact: {exc}"
        return json.dumps({"patch_path": str(patch_path), "bytes": patch_path.stat().st_size}, sort_keys=True)

    def get_workspace_info(self, workspace_id: str) -> str:
        """Return public workspace metadata plus current dirty state."""
        workspace = self._workspace_tuple(workspace_id)
        if isinstance(workspace, str):
            return workspace
        metadata, _workspace_dir = workspace
        repo_dir = Path(metadata["repo_dir"]).resolve()
        metadata = dict(metadata)
        metadata["exists"] = repo_dir.exists()
        metadata["expired"] = _is_expired(metadata.get("expires_at"))
        metadata["dirty"] = _workspace_dirty(repo_dir)
        return json.dumps(_public_metadata(metadata), sort_keys=True)

    def create_handoff(self, workspace_id: str, instructions: str, suggested_command: str | None = None) -> str:
        """Create a non-executing handoff descriptor for a separate coding sandbox."""
        workspace = self._workspace_tuple(workspace_id)
        if isinstance(workspace, str):
            return workspace
        metadata, workspace_dir = workspace
        handoff = {
            "workspace_id": workspace_id,
            "repo": metadata.get("repo"),
            "repo_dir": metadata.get("repo_dir"),
            "instructions": instructions,
            "suggested_command": suggested_command,
            "execution_performed": False,
            "required_executor": "coding_sandbox",
            "security_notes": [
                "repo_workspace did not execute commands",
                "repo_workspace did not fetch network content",
                "executor must enforce its own timeout, secret, and network policy",
            ],
        }
        handoff_path = (workspace_dir / "artifacts" / "handoff.json").resolve()
        if not _is_relative_to(handoff_path, (workspace_dir / "artifacts").resolve()):
            return "Error: handoff path escapes artifacts directory."
        handoff_path.write_text(json.dumps(handoff, indent=2, sort_keys=True), encoding="utf-8")
        return json.dumps({"handoff_path": str(handoff_path), "handoff": handoff}, sort_keys=True)

    def cleanup_workspace(self, workspace_id: str, confirm_write: bool = False) -> str:
        """Delete one confined workspace directory."""
        if not confirm_write:
            return _write_confirmation_error("cleanup_workspace")
        try:
            workspace_dir = self._workspace_dir(_validate_workspace_id(workspace_id))
        except ValueError as exc:
            return f"Error: {exc}"
        if not workspace_dir.exists():
            return f"Workspace {workspace_id} already absent."
        if not _is_relative_to(workspace_dir, self.workspace_root):
            return "Error: workspace path escapes workspace_root."
        shutil.rmtree(workspace_dir)
        return f"Deleted workspace {workspace_id}"

    def _authorize_repo(self, repo: str) -> str:
        try:
            repo_name = _validate_repo(repo)
        except ValueError as exc:
            return f"Error: {exc}"
        if _matches_any(repo_name, self.denied_repos):
            return f"Error: repository is explicitly denied: {repo_name}"
        if not _matches_any(repo_name, self.allowed_repos):
            return f"Error: repository is not allowed: {repo_name}"
        return repo_name

    def _workspace_dir(self, workspace_id: str) -> Path:
        return (self.workspace_root / workspace_id).resolve()

    def _validate_source_path(self, source_path: str) -> Path:
        source = Path(source_path).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise ValueError("source_path must be an existing directory.")
        if not self.allowed_source_roots:
            raise ValueError("source_path is disabled unless allowed_source_roots is configured.")
        if not any(_is_relative_to(source, root) for root in self.allowed_source_roots):
            raise ValueError("source_path is outside allowed_source_roots.")
        _validate_source_tree_safe(source)
        return source

    def _repo_dir_for_workspace(self, workspace_id: str) -> Path | str:
        workspace = self._workspace_tuple(workspace_id)
        if isinstance(workspace, str):
            return workspace
        metadata, _workspace_dir = workspace
        return Path(metadata["repo_dir"]).resolve()

    def _workspace_tuple(self, workspace_id: str) -> tuple[dict[str, Any], Path] | str:
        try:
            workspace_id = _validate_workspace_id(workspace_id)
        except ValueError as exc:
            return f"Error: {exc}"
        workspace_dir = self._workspace_dir(workspace_id)
        if not _is_relative_to(workspace_dir, self.workspace_root):
            return "Error: workspace path escapes workspace_root."
        metadata_path = workspace_dir / "workspace.json"
        if not metadata_path.exists():
            return f"Error: unknown workspace: {workspace_id}"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Error reading workspace metadata: {exc}"
        validation_error = _validate_workspace_metadata(metadata, workspace_dir)
        if validation_error:
            return validation_error
        return metadata, workspace_dir

    def _resolve_repo_file(self, workspace_id: str, path: str, *, must_exist: bool) -> Path | str:
        repo_dir = self._repo_dir_for_workspace(workspace_id)
        if isinstance(repo_dir, str):
            return repo_dir
        if _is_hidden_git_path(path):
            return "Error: direct .git access is not allowed."
        target = (repo_dir / path).resolve()
        if not _is_relative_to(target, repo_dir):
            return "Error: path escapes workspace repo."
        if must_exist and not target.is_file():
            return "Error: file does not exist."
        return target

    def _write_metadata(self, workspace_dir: Path, metadata: dict[str, Any]) -> None:
        (workspace_dir / "workspace.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _normalize_patterns(patterns: list[str] | tuple[str, ...], *, option_name: str) -> tuple[str, ...]:
    if not patterns:
        raise ValueError(f"{option_name} must not be empty.")
    normalized = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise ValueError(f"{option_name} entries must be strings.")
        pattern = pattern.strip()
        if not pattern:
            raise ValueError(f"{option_name} entries must not be empty.")
        if pattern in {"*", "*/*"} or pattern.startswith("*/") or pattern.endswith("*"):
            raise ValueError(f"Dangerously broad repository pattern is not allowed: {pattern}")
        if "*" in pattern and not pattern.endswith("/*"):
            raise ValueError(f"Only owner/* wildcard repository patterns are supported: {pattern}")
        if pattern.endswith("/*"):
            owner = pattern[:-2]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
                raise ValueError(f"Invalid repository owner pattern: {pattern}")
        else:
            _validate_repo(pattern)
        normalized.append(pattern)
    return tuple(normalized)


def _validate_repo(repo: str) -> str:
    repo = repo.strip()
    if not _GITHUB_REPO_RE.fullmatch(repo):
        raise ValueError("repo must be an owner/name GitHub repository identifier.")
    return repo


def _matches_any(repo: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(repo, pattern) for pattern in patterns)


def _validate_workspace_id(workspace_id: str) -> str:
    if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise ValueError("workspace_id must be 1-81 chars of lowercase letters, digits, dot, underscore, or dash.")
    return workspace_id


def _new_workspace_id(repo: str) -> str:
    slug = repo.lower().replace("/", "-")
    return f"{slug}-{uuid.uuid4().hex[:10]}"


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _relative_repo_path(repo_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_dir.resolve()).as_posix()


def _is_hidden_git_path(path: str) -> bool:
    return path == ".git" or path.startswith(".git/") or "/.git/" in path


def _safe_subprocess_env() -> dict[str, str]:
    denied_prefixes = ("GITHUB_", "GH_", "GIT_", "SSH_", "AWS_", "AZURE_", "GOOGLE_", "OPENAI_", "ANTHROPIC_")
    env = {key: value for key, value in os.environ.items() if not key.startswith(denied_prefixes)}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
        }
    )
    return env


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env=_safe_subprocess_env(),
        check=False,
    )


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
        "repo_identity": expected_repo,
        "source_ref": source_ref,
        "source_head_sha": None,
        "origin_url": None,
    }
    if not (source / ".git").is_dir():
        return provenance

    _verify_git_checkout(source)
    head_result = _run_git(["rev-parse", "--verify", "HEAD"], cwd=source)
    if head_result.returncode != 0:
        raise ValueError("source_path .git checkout has no verifiable HEAD commit.")
    origin_result = _run_git(["config", "--get", "remote.origin.url"], cwd=source)
    origin_url = origin_result.stdout.strip() if origin_result.returncode == 0 else ""
    redacted_origin = _redact_origin_url(origin_url) if origin_url else None
    repo_identity = _repo_identity_from_origin(redacted_origin) if redacted_origin else None
    if repo_identity and repo_identity != expected_repo:
        raise ValueError(f"source_path git origin does not match requested repo: {repo_identity}")
    ref_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=source)
    detected_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else ""
    if not detected_ref or detected_ref == "HEAD":
        detected_ref = source_ref or None

    provenance.update(
        {
            "source_type": "local_git_checkout",
            "repo_identity": repo_identity or expected_repo,
            "source_ref": source_ref or detected_ref,
            "source_head_sha": head_result.stdout.strip(),
            "origin_url": redacted_origin,
        }
    )
    return provenance


def _verify_git_checkout(source: Path) -> None:
    result = _run_git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir"], cwd=source)
    if result.returncode != 0:
        raise ValueError("source_path .git directory is not a valid git checkout.")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("source_path .git checkout could not be verified.")
    toplevel = Path(lines[0]).resolve()
    git_dir = Path(lines[1]).resolve()
    if toplevel != source.resolve() or git_dir != (source / ".git").resolve():
        raise ValueError("source_path must be the root of a non-linked local git checkout.")


def _redact_origin_url(origin_url: str) -> str:
    if not origin_url:
        return origin_url
    if "@" in origin_url and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", origin_url):
        parsed = urlsplit(origin_url)
        hostname = parsed.hostname or ""
        netloc = hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", origin_url):
        parsed = urlsplit(origin_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return origin_url.split("?", 1)[0].split("#", 1)[0]


def _repo_identity_from_origin(origin_url: str | None) -> str | None:
    if not origin_url:
        return None
    candidate = origin_url.rstrip("/")
    if candidate.startswith("git@github.com:"):
        path = candidate.split(":", 1)[1]
    else:
        parsed = urlsplit(candidate)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if _GITHUB_REPO_RE.fullmatch(path) else None


def _copy_source_tree(source: Path, destination: Path) -> None:
    _validate_source_tree_safe(source)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False, ignore_dangling_symlinks=False)
        else:
            shutil.copy2(child, target)
    _validate_source_tree_safe(destination)


def _validate_source_tree_safe(source: Path) -> None:
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        for name in dirs + files:
            candidate = root_path / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot inspect source tree entry {candidate}: {exc}") from exc
            if stat.S_ISLNK(mode):
                raise ValueError(f"source tree contains symlink, which is not allowed: {candidate}")
            if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                raise ValueError(f"source tree contains special file, which is not allowed: {candidate}")


def _bounded_output(stdout: str, stderr: str = "") -> str:
    output = stdout if stdout else stderr
    if len(output.encode("utf-8")) <= _MAX_COMMAND_OUTPUT_BYTES:
        return output.strip()
    encoded = output.encode("utf-8")[:_MAX_COMMAND_OUTPUT_BYTES]
    return encoded.decode("utf-8", errors="replace").rstrip() + "\n[truncated]"


def _new_file_patch(repo_dir: Path, rel: str) -> str:
    path = repo_dir / rel
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in data:
        return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nBinary files /dev/null and b/{rel} differ\n"
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    body = "".join(f"+{line}" if line.endswith("\n") else f"+{line}\n" for line in lines)
    return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _tree_snapshot_patch(repo_dir: Path) -> str:
    parts = []
    for path in sorted(repo_dir.rglob("*")):
        if path.is_file():
            rel = _relative_repo_path(repo_dir, path)
            if not _is_hidden_git_path(rel):
                parts.append(_new_file_patch(repo_dir, rel))
    return "\n".join(parts)


def _workspace_dirty(repo_dir: Path) -> bool:
    if not repo_dir.exists():
        return False
    if not (repo_dir / ".git").is_dir():
        return any(repo_dir.rglob("*"))
    result = _run_git(["status", "--porcelain"], cwd=repo_dir)
    return bool(result.stdout.strip())


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "workspace_id",
        "repo",
        "ref",
        "created_at",
        "expires_at",
        "workspace_dir",
        "repo_dir",
        "artifacts_dir",
        "network_policy",
        "execution_policy",
        "provenance",
        "audit",
        "exists",
        "expired",
        "dirty",
    }
    return {key: metadata[key] for key in allowed_keys if key in metadata}


def _validate_workspace_metadata(metadata: dict[str, Any], workspace_dir: Path) -> str | None:
    repo_dir = Path(str(metadata.get("repo_dir", ""))).resolve()
    expected_repo_dir = (workspace_dir / "repo").resolve()
    if repo_dir != expected_repo_dir:
        return "Error: workspace metadata repo_dir does not match expected confined path."

    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        return "Error: workspace metadata is missing required provenance."
    source_type = provenance.get("source_type")
    if source_type not in {"empty_workspace", "local_copy", "local_git_checkout"}:
        return "Error: workspace metadata provenance has invalid source_type."
    if source_type != "empty_workspace":
        source_path = provenance.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            return "Error: workspace metadata provenance is missing source_path."
        try:
            Path(source_path).resolve()
        except OSError:
            return "Error: workspace metadata provenance has invalid source_path."

    audit = metadata.get("audit")
    if not isinstance(audit, dict):
        return "Error: workspace metadata is missing required audit fields."
    if audit.get("created_by_tool") != "repo_workspace":
        return "Error: workspace metadata audit has invalid created_by_tool."
    if audit.get("network_performed") is not False or audit.get("execution_performed") is not False:
        return "Error: workspace metadata audit violates repo_workspace non-execution policy."
    if audit.get("writes_require_confirmation") is not True:
        return "Error: workspace metadata audit is missing write confirmation policy."
    return None


def _write_confirmation_error(action: str) -> str:
    return f"Error: {action} requires confirm_write=true because it modifies workspace state."