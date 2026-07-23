"""Repository sandbox tools for safe pre-seeded local inspect/edit/test workflows."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from agno.tools import Toolkit

from mindroom.custom_tools.coding import CodingTools
from mindroom.tools.path_safety import resolve_base_dir_path

_DEFAULT_ALLOWED_REPOS = ("schmits/repo-sandbox-fixture",)
_DEFAULT_DENIED_REPOS = (
    "schmits/prod",
    "schmits/production",
    "schmits/secrets",
    "schmits/security",
)
_DEFAULT_ALLOWED_TEST_COMMANDS = ("pytest -q", "python -m pytest -q", "npm test", "npm run test", "pnpm test")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_SSH_RE = re.compile(r"^(?:git@|ssh://git@)github\.com[:/](?P<path>[^?#]+)$", re.IGNORECASE)
_MAX_COMMAND_OUTPUT_BYTES = 60_000
_DEFAULT_TEST_TIMEOUT_SECONDS = 120


class RepoSandboxTools(Toolkit):
    """Safe repository sandbox wrapper for non-production coding fixtures.

    The wrapper intentionally avoids generic shell access and authenticated
    GitHub access: git operations are limited to local verification, status, and
    diff commands. Repository names are allowlisted, filesystem access is
    confined to one repository directory under ``sandbox_root``, and mutating
    edit/test operations require ``confirm_write=True``.
    """

    def __init__(
        self,
        sandbox_root: str | None = None,
        allowed_repos: list[str] | str | None = None,
        denied_repos: list[str] | str | None = None,
        allowed_test_commands: list[str] | str | None = None,
        default_repo: str = "schmits/repo-sandbox-fixture",
    ) -> None:
        self.sandbox_root = Path(sandbox_root).resolve() if sandbox_root else (Path.cwd() / "repo_sandbox").resolve()
        self.allowed_repos = _normalize_repo_patterns(allowed_repos, fallback=_DEFAULT_ALLOWED_REPOS, field_name="allowed_repos")
        self.denied_repos = _normalize_repo_patterns(denied_repos, fallback=_DEFAULT_DENIED_REPOS, field_name="denied_repos")
        self.allowed_test_commands = _normalize_string_list(allowed_test_commands) or list(_DEFAULT_ALLOWED_TEST_COMMANDS)
        self.default_repo = default_repo
        super().__init__(
            name="repo_sandbox",
            tools=[
                self.clone_or_update,
                self.status,
                self.list_files,
                self.read_file,
                self.grep,
                self.edit_file,
                self.write_file,
                self.run_tests,
            ],
        )

    def clone_or_update(
        self,
        repo: str | None = None,
        ref: str | None = None,
        confirm_write: bool = False,
    ) -> str:
        """Verify an allowlisted, pre-seeded local repository checkout.

        This compatibility-named function no longer clones, fetches, checks out,
        or authenticates to GitHub. Repositories must already exist under
        ``sandbox_root`` using the configured owner/name slug directory.

        Args:
            repo: GitHub repository in owner/name form. Defaults to the configured fixture repository.
            ref: Optional branch, tag, or commit-like ref that must already resolve to the current HEAD.
            confirm_write: Accepted for backward compatibility; no write is performed.

        Returns:
            A status message with the sandbox-relative repository path.

        """
        try:
            repo_name = self._validate_repo(repo)
        except ValueError as exc:
            return f"Error: {exc}"
        repo_dir = self._repo_dir(repo_name)
        if not repo_dir.exists():
            return _preseeded_repo_missing_error()
        if not (repo_dir / ".git").is_dir():
            return f"Error: sandbox path exists but is not a git repository: {repo_dir}"

        inside = self._run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return _format_completed_process("git repository verification failed", inside)

        head = self._run_git(["rev-parse", "HEAD"], cwd=repo_dir)
        if head.returncode != 0:
            return _format_completed_process("git HEAD verification failed", head)
        head_sha = head.stdout.strip()

        if ref:
            if not _valid_git_ref(ref):
                return "Error: ref contains unsupported characters. Use a branch, tag, or commit-like ref."
            ref_result = self._run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_dir)
            if ref_result.returncode != 0:
                return _format_completed_process("git ref verification failed", ref_result)
            ref_sha = ref_result.stdout.strip()
            if ref_sha != head_sha:
                short_head = head_sha[:7]
                short_ref = ref_sha[:7]
                return f"Error: pre-seeded checkout is at {short_head}, but ref '{ref}' resolves to {short_ref}. Seed the expected ref locally before using repo_sandbox."

        suffix = f" at {head_sha[:7]}" if head_sha else ""
        return f"Repository ready: {_repo_slug(repo_name)}/{suffix}"

    def status(self, repo: str | None = None) -> str:
        """Return git status and a compact diff summary for an allowlisted sandbox repository."""
        repo_dir = self._existing_repo_dir(repo)
        if isinstance(repo_dir, str):
            return repo_dir
        status_result = self._run_git(["status", "--short", "--branch"], cwd=repo_dir)
        diff_result = self._run_git(["diff", "--stat", "--"], cwd=repo_dir)
        parts = ["# git status", status_result.stdout or status_result.stderr]
        if diff_result.stdout:
            parts.extend(["\n# git diff --stat", diff_result.stdout])
        return _truncate_output("\n".join(parts).strip())

    def list_files(self, repo: str | None = None, pattern: str = "**/*", limit: int = 500) -> str:
        """List repository files matching a glob pattern, excluding the .git directory."""
        repo_dir = self._existing_repo_dir(repo)
        if isinstance(repo_dir, str):
            return repo_dir
        if limit <= 0:
            return "Error: limit must be positive."
        matches: list[str] = []
        for path in repo_dir.rglob("*"):
            if ".git" in path.relative_to(repo_dir).parts:
                continue
            rel = path.relative_to(repo_dir).as_posix() + ("/" if path.is_dir() else "")
            if fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
            if len(matches) >= limit:
                break
        if not matches:
            return "No files found."
        suffix = f"\n[limited to {limit} entries]" if len(matches) >= limit else ""
        return "\n".join(matches) + suffix

    def read_file(self, path: str, repo: str | None = None, offset: int | None = None, limit: int | None = None) -> str:
        """Read a file from an allowlisted sandbox repository with line numbers."""
        coding = self._coding_tools(repo)
        if isinstance(coding, str):
            return coding
        return coding.read_file(path, offset=offset, limit=limit)

    def grep(
        self,
        pattern: str,
        repo: str | None = None,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int = 100,
    ) -> str:
        """Search file contents inside an allowlisted sandbox repository."""
        coding = self._coding_tools(repo)
        if isinstance(coding, str):
            return coding
        return coding.grep(pattern, path=path, glob=glob, ignore_case=ignore_case, literal=literal, context=context, limit=limit)

    def edit_file(self, path: str, old_text: str, new_text: str, repo: str | None = None, confirm_write: bool = False) -> str:
        """Replace a unique text occurrence in a sandbox repository file. Requires confirm_write=true."""
        if not confirm_write:
            return _write_confirmation_error("edit_file")
        coding = self._coding_tools(repo)
        if isinstance(coding, str):
            return coding
        return coding.edit_file(path, old_text, new_text)

    def write_file(self, path: str, content: str, repo: str | None = None, confirm_write: bool = False) -> str:
        """Write a file inside a sandbox repository. Requires confirm_write=true."""
        if not confirm_write:
            return _write_confirmation_error("write_file")
        coding = self._coding_tools(repo)
        if isinstance(coding, str):
            return coding
        return coding.write_file(path, content)

    def run_tests(
        self,
        command: str = "pytest -q",
        repo: str | None = None,
        timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
        confirm_write: bool = False,
    ) -> str:
        """Run an allowlisted test command inside a sandbox repository. Requires confirm_write=true.

        Test commands are treated as mutating because they may create caches or
        build artifacts. Commands are split with shlex and run without a shell.
        """
        if not confirm_write:
            return _write_confirmation_error("run_tests")
        if command not in self.allowed_test_commands:
            allowed = ", ".join(repr(item) for item in self.allowed_test_commands)
            return f"Error: test command is not allowlisted. Allowed commands: {allowed}."
        if timeout_seconds <= 0 or timeout_seconds > 600:
            return "Error: timeout_seconds must be between 1 and 600."
        repo_dir = self._existing_repo_dir(repo)
        if isinstance(repo_dir, str):
            return repo_dir
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"Error parsing command: {exc}"
        if not argv:
            return "Error: command must be non-empty."
        try:
            result = subprocess.run(
                argv,
                cwd=repo_dir,
                env=_safe_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            return f"Error: executable not found: {exc.filename}"
        except subprocess.TimeoutExpired as exc:
            partial = "\n".join(part for part in (exc.stdout, exc.stderr) if isinstance(part, str))
            return _truncate_output(f"Command timed out after {timeout_seconds}s.\n{partial}".strip())
        return _format_completed_process(f"test command exited {result.returncode}", result)

    def _validate_repo(self, repo: str | None) -> str:
        repo_name = _canonical_github_repo(repo or self.default_repo)
        if _matches_repo_patterns(repo_name, self.denied_repos):
            denied = ", ".join(self.denied_repos)
            raise ValueError(f"repository '{repo_name}' is explicitly denied. Denied repositories: {denied}.")
        if not _matches_repo_patterns(repo_name, self.allowed_repos):
            allowed = ", ".join(self.allowed_repos)
            raise ValueError(f"repository '{repo_name}' is not allowlisted. Allowed repositories: {allowed}.")
        return repo_name

    def _repo_dir(self, repo: str) -> Path:
        return resolve_base_dir_path(self.sandbox_root, _repo_slug(repo), restrict_to_base_dir=True)

    def _existing_repo_dir(self, repo: str | None) -> Path | str:
        try:
            repo_name = self._validate_repo(repo)
        except ValueError as exc:
            return f"Error: {exc}"
        repo_dir = self._repo_dir(repo_name)
        if not (repo_dir / ".git").is_dir():
            return _preseeded_repo_missing_error()
        return repo_dir

    def _coding_tools(self, repo: str | None) -> CodingTools | str:
        repo_dir = self._existing_repo_dir(repo)
        if isinstance(repo_dir, str):
            return repo_dir
        return CodingTools(base_dir=str(repo_dir), restrict_to_base_dir=True)

    @staticmethod
    def _run_git(args: list[str], *, cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = _safe_subprocess_env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )


def _normalize_string_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _normalize_repo_patterns(value: list[str] | str | None, *, fallback: tuple[str, ...], field_name: str) -> list[str]:
    raw_patterns = _normalize_string_list(value) or list(fallback)
    normalized: list[str] = []
    for raw in raw_patterns:
        try:
            if raw.endswith("*"):
                if not raw.endswith("/*"):
                    raise ValueError("wildcard patterns must end with '/*'.")
                owner_repo = _canonical_github_repo(f"{raw.removesuffix('/*')}/placeholder")
                canonical = f"{owner_repo.split('/', 1)[0]}/*"
            else:
                canonical = _canonical_github_repo(raw)
        except ValueError as exc:
            raise ValueError(f"{field_name}: invalid repository pattern '{raw}': {exc}") from exc
        normalized.append(canonical)
    return normalized


def _canonical_github_repo(repo: str) -> str:
    repo_name = repo.strip()
    if not repo_name:
        raise ValueError("repo must be non-empty.")

    ssh_match = _GITHUB_SSH_RE.fullmatch(repo_name)
    if ssh_match:
        repo_name = ssh_match.group("path")
    elif "://" in repo_name:
        parsed = urlparse(repo_name)
        if parsed.scheme not in {"https", "http"}:
            raise ValueError("repo URL must use https://github.com/ or owner/name form.")
        if parsed.hostname != "github.com":
            raise ValueError("repo URL host must be github.com.")
        repo_name = parsed.path
    elif repo_name.lower().startswith("github.com/"):
        repo_name = repo_name[len("github.com/") :]
    elif ":" in repo_name or "@" in repo_name:
        raise ValueError("repo must be a GitHub owner/name, https URL, or git@github.com SSH URL.")

    repo_name = repo_name.strip("/")
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    parts = repo_name.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError("repo must identify exactly one GitHub repository in owner/name form.")
    if any(part in {".", ".."} for part in parts) or not _GITHUB_REPO_RE.fullmatch(repo_name):
        raise ValueError("repo contains unsupported characters.")
    return f"{parts[0].lower()}/{parts[1]}"


def _matches_repo_patterns(repo: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/*"):
            owner = pattern.removesuffix("/*")
            if repo.startswith(f"{owner}/"):
                return True
        elif repo == pattern:
            return True
    return False


def _repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def _valid_git_ref(ref: str) -> bool:
    return bool(ref) and all(ch.isalnum() or ch in "._/-" for ch in ref) and ".." not in ref and not ref.startswith("-")


def _looks_like_commit(ref: str) -> bool:
    return len(ref) >= 7 and all(ch in "0123456789abcdefABCDEF" for ch in ref)


def _safe_subprocess_env() -> dict[str, str]:
    keys = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    return {key: value for key, value in os.environ.items() if key in keys}


def _preseeded_repo_missing_error() -> str:
    return "Error: repository is not available locally. Pre-seed the allowlisted checkout under sandbox_root before using repo_sandbox."


def _write_confirmation_error(action: str) -> str:
    return f"Error: {action} writes inside the repository sandbox. Re-run with confirm_write=true after confirming the target repo/path/command."


def _truncate_output(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_COMMAND_OUTPUT_BYTES:
        return text
    return encoded[:_MAX_COMMAND_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"


def _redact_text(text: str, secrets: list[str] | None = None) -> str:
    redacted = text
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _format_completed_process(label: str, result: subprocess.CompletedProcess[str], *, secrets: list[str] | None = None) -> str:
    parts = [label]
    if result.stdout:
        parts.extend(["\nstdout:", result.stdout])
    if result.stderr:
        parts.extend(["\nstderr:", result.stderr])
    if not result.stdout and not result.stderr:
        parts.append("\n(no output)")
    return _truncate_output(_redact_text("".join(parts).strip(), secrets))
