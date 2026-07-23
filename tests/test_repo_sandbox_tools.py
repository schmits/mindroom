"""Tests for repo_sandbox custom tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mindroom.custom_tools.repo_sandbox import RepoSandboxTools
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True, text=True)


def test_repo_sandbox_metadata_registered() -> None:
    metadata = BUILTIN_TOOL_METADATA["repo_sandbox"]
    assert metadata.default_execution_target.value == "worker"
    assert metadata.consumes_workspace_paths is True
    assert "clone_or_update" in metadata.function_names


def test_write_operations_require_confirmation(tmp_path: Path) -> None:
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    assert "Pre-seed the allowlisted checkout" in tools.clone_or_update()
    assert "confirm_write=true" in tools.edit_file("README.md", "a", "b")
    assert "confirm_write=true" in tools.write_file("README.md", "content")
    assert "confirm_write=true" in tools.run_tests()


def test_rejects_unallowlisted_repo(tmp_path: Path) -> None:
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    assert "not allowlisted" in tools.status(repo="octocat/Hello-World")


def test_scoped_repo_pattern_allows_canonical_github_inputs(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__example"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["github.com/schmits/*"])

    assert tools.status(repo="github.com/schmits/example").startswith("# git status")
    assert tools.status(repo="https://github.com/schmits/example.git").startswith("# git status")
    assert tools.status(repo="git@github.com:schmits/example.git").startswith("# git status")


def test_scoped_repo_pattern_denies_malformed_and_lookalike_inputs(tmp_path: Path) -> None:
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["github.com/schmits/*"])

    denied_inputs = [
        "octocat/example",
        "github.com/schmitsfoo/example",
        "https://evil.test/schmits/example",
        "https://github.evil.test/schmits/example",
        "https://gitlab.com/schmits/example",
        "git@gitlab.com:schmits/example.git",
        "schmits/../secrets",
        "https://github.com/schmits/example/../../secrets",
    ]

    for repo in denied_inputs:
        result = tools.status(repo=repo)
        assert result.startswith("Error:"), repo
        assert "not allowlisted" in result or "repo" in result, repo


def test_repo_deny_rules_take_precedence_over_scoped_allow_patterns(tmp_path: Path) -> None:
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["github.com/schmits/*"])

    for repo in ["schmits/prod", "schmits/production", "schmits/secrets", "schmits/security"]:
        result = tools.status(repo=repo)
        assert "explicitly denied" in result


def test_file_access_confined_to_preseeded_repo(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    assert "hello" in tools.read_file("README.md")
    assert "outside base_dir" in tools.read_file("../outside.txt")


def test_edit_and_status_inside_existing_repo(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    result = tools.edit_file("README.md", "hello", "hi", confirm_write=True)

    assert "Applied edit" in result
    assert "README.md" in tools.status()


def test_run_tests_uses_exact_allowlist(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(
        sandbox_root=str(tmp_path),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_test_commands=["python -c 'print(123)'"],
    )

    assert "not allowlisted" in tools.run_tests("python -c 'print(456)'", confirm_write=True)
    assert "123" in tools.run_tests("python -c 'print(123)'", confirm_write=True)


def test_mode_separation_keeps_repo_and_test_allowlists_independent(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__example"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(
        sandbox_root=str(tmp_path),
        allowed_repos=["github.com/schmits/*"],
        allowed_test_commands=["python -c 'print(123)'"],
    )

    assert "# git status" in tools.status(repo="schmits/example")
    assert "not allowlisted" in tools.run_tests("pytest -q", repo="schmits/example", confirm_write=True)
    assert "123" in tools.run_tests("python -c 'print(123)'", repo="schmits/example", confirm_write=True)
    assert "not allowlisted" in tools.run_tests("python -c 'print(123)'", repo="octocat/example", confirm_write=True)


def test_clone_or_update_verifies_preseeded_repo_without_clone_fetch_or_auth(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    calls: list[list[str]] = []
    real_run_git = RepoSandboxTools._run_git

    def spy_run_git(args: list[str], **kwargs: object):
        calls.append(args)
        assert args[0] not in {"clone", "fetch", "checkout"}
        extra_env = kwargs.get("extra_env")
        assert not extra_env
        return real_run_git(args, **kwargs)

    monkeypatch.setenv("REPO_SANDBOX_GITHUB_TOKEN", "sandbox-token")
    monkeypatch.setenv("REPO_SANDBOX_GITHUB_APP_TOKEN", "app-token")
    monkeypatch.setattr(RepoSandboxTools, "_run_git", staticmethod(spy_run_git))
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    result = tools.clone_or_update(confirm_write=True)

    assert result.startswith("Repository ready: schmits__repo-sandbox-fixture/ at ")
    assert calls
    assert [call[0] for call in calls] == ["rev-parse", "rev-parse"]


def test_clone_or_update_does_not_require_confirmation_for_local_verification(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    assert tools.clone_or_update().startswith("Repository ready:")


def test_clone_or_update_missing_repo_reports_preseed_requirement(tmp_path: Path) -> None:
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    result = tools.clone_or_update(confirm_write=True)

    assert "Pre-seed the allowlisted checkout" in result


def test_clone_or_update_ref_must_match_current_preseeded_head(tmp_path: Path) -> None:
    repo_dir = tmp_path / "schmits__repo-sandbox-fixture"
    _init_repo(repo_dir)
    subprocess.run(["git", "checkout", "-b", "other"], cwd=repo_dir, check=True, capture_output=True, text=True)
    (repo_dir / "README.md").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "other"], cwd=repo_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True, capture_output=True, text=True)
    tools = RepoSandboxTools(sandbox_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    result = tools.clone_or_update(ref="other", confirm_write=True)

    assert "pre-seeded checkout is at" in result
    assert "Seed the expected ref locally" in result


def test_safe_subprocess_env_filters_git_auth_and_tokens(monkeypatch) -> None:
    monkeypatch.setenv("REPO_SANDBOX_GITHUB_TOKEN", "sandbox-token")
    monkeypatch.setenv("REPO_SANDBOX_GITHUB_APP_TOKEN", "app-token")
    monkeypatch.setenv("GITHUB_TOKEN", "generic-token")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    env = RepoSandboxTools._run_git.__globals__["_safe_subprocess_env"]()

    assert "REPO_SANDBOX_GITHUB_TOKEN" not in env
    assert "REPO_SANDBOX_GITHUB_APP_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert "GIT_TERMINAL_PROMPT" not in env
