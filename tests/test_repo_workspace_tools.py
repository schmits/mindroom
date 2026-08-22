"""Tests for repo_workspace custom tools."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mindroom.custom_tools.repo_workspace import RepoWorkspaceTools
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True, text=True)


def _create_workspace(tools: RepoWorkspaceTools, workspace_id: str = "ws-test", source_path: Path | None = None) -> dict[str, object]:
    result = tools.create_workspace(
        repo="schmits/repo-sandbox-fixture",
        ref="main",
        source_path=str(source_path) if source_path else None,
        workspace_id=workspace_id,
        confirm_write=True,
    )
    payload = json.loads(result)
    assert payload["status"] == "created"
    return payload


def test_repo_workspace_metadata_registered() -> None:
    metadata = BUILTIN_TOOL_METADATA["repo_workspace"]

    assert metadata.default_execution_target.value == "worker"
    assert metadata.consumes_workspace_paths is True
    assert "create_workspace" in metadata.function_names
    assert "handoff_to_coding_sandbox" in metadata.function_names


def test_create_and_destroy_require_confirmation(tmp_path: Path) -> None:
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    assert "confirm_write=true" in tools.create_workspace(workspace_id="ws-test")
    _create_workspace(tools)
    assert "confirm_write=true" in tools.destroy_workspace("ws-test")
    assert tools.destroy_workspace("ws-test", confirm_write=True) == "Destroyed workspace ws-test."


def test_rejects_unallowlisted_and_denied_repositories(tmp_path: Path) -> None:
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/*"], denied_repos=["schmits/prod"])

    assert "not allowlisted" in tools.create_workspace(repo="octocat/Hello-World", workspace_id="ws-a", confirm_write=True)
    assert "explicitly denied" in tools.create_workspace(repo="schmits/prod", workspace_id="ws-b", confirm_write=True)


def test_rejects_dangerously_broad_repo_patterns(tmp_path: Path) -> None:
    for pattern in ["*", "*/*", "*/repo", "owner/repo*"]:
        try:
            RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=[pattern])
        except ValueError as exc:
            assert "repository pattern" in str(exc)
        else:  # pragma: no cover - explicit failure path
            raise AssertionError(f"accepted overly broad pattern: {pattern}")


def test_source_path_requires_allowlisted_source_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    tools = RepoWorkspaceTools(
        workspace_root=str(tmp_path / "workspaces"),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_source_roots=[str(tmp_path / "other")],
    )

    result = tools.create_workspace(source_path=str(source), workspace_id="ws-test", confirm_write=True)

    assert "outside allowed_source_roots" in result


def test_create_workspace_rejects_network_request(tmp_path: Path) -> None:
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    result = tools.create_workspace(workspace_id="ws-test", allow_network=True, confirm_write=True)

    assert "network materialization is not implemented" in result


def test_create_workspace_rejects_nested_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    nested = source / "nested"
    nested.mkdir()
    (nested / "secret-link").symlink_to(tmp_path / "outside-secret")
    tools = RepoWorkspaceTools(
        workspace_root=str(tmp_path / "workspaces"),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_source_roots=[str(tmp_path)],
    )

    result = tools.create_workspace(source_path=str(source), workspace_id="ws-test", confirm_write=True)

    assert "contains symlinks" in result


def test_create_workspace_copies_source_and_records_policy_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    tools = RepoWorkspaceTools(
        workspace_root=str(tmp_path / "workspaces"),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_source_roots=[str(tmp_path)],
    )

    payload = _create_workspace(tools, source_path=source)
    info = json.loads(tools.get_workspace_info("ws-test"))

    assert payload["workspace"]["network_policy"]["network_performed"] is False
    assert info["execution_policy"]["allow_arbitrary_execution"] is False
    assert info["repo"] == "schmits/repo-sandbox-fixture"
    assert "README.md" in tools.list_files("ws-test", pattern="*")
    assert "hello" in tools.read_file("ws-test", "README.md")


def test_file_operations_are_confined_to_workspace(tmp_path: Path) -> None:
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])
    _create_workspace(tools)

    assert "outside base_dir" in tools.read_file("ws-test", "../outside.txt")
    assert "confirm_write=true" in tools.write_file("ws-test", "README.md", "hello\n")
    assert "Wrote README.md" in tools.write_file("ws-test", "README.md", "hello\n", confirm_write=True)
    assert "direct access to .git internals" in tools.read_file("ws-test", ".git/config")


def test_status_diff_apply_patch_and_export_patch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    tools = RepoWorkspaceTools(
        workspace_root=str(tmp_path / "workspaces"),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_source_roots=[str(tmp_path)],
    )
    _create_workspace(tools, source_path=source)

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-hello
+hi
"""

    assert "confirm_write=true" in tools.apply_patch("ws-test", patch)
    assert tools.apply_patch("ws-test", patch, confirm_write=True) == "Patch applied."
    assert "README.md" in tools.get_status("ws-test")
    diff = tools.get_diff("ws-test")
    assert "-hello" in diff
    assert "+hi" in diff
    exported = json.loads(tools.export_patch("ws-test", confirm_write=True))
    assert Path(exported["artifact"]).is_file()


def test_git_diff_disables_external_diff_execution(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    marker = tmp_path / "external-diff-ran"
    script = tmp_path / "external_diff.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external", f"python {script}"], cwd=source, check=True)
    (source / "README.md").write_text("changed\n", encoding="utf-8")
    tools = RepoWorkspaceTools(
        workspace_root=str(tmp_path / "workspaces"),
        allowed_repos=["schmits/repo-sandbox-fixture"],
        allowed_source_roots=[str(tmp_path)],
    )
    _create_workspace(tools, source_path=source)

    diff = tools.get_diff("ws-test")

    assert "changed" in diff
    assert not marker.exists()


def test_handoff_to_coding_sandbox_does_not_execute(tmp_path: Path) -> None:
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])
    _create_workspace(tools)

    descriptor = json.loads(tools.handoff_to_coding_sandbox("ws-test", command="pytest -q", timeout_seconds=60))

    assert descriptor["type"] == "coding_sandbox_handoff"
    assert descriptor["command"] == "pytest -q"
    assert descriptor["execution_policy"]["requires_external_execution_substrate"] == "coding_sandbox"
    assert descriptor["execution_policy"]["authorization_status"] == "not_authorized_by_repo_workspace"
    assert descriptor["execution_policy"]["no_ambient_secrets"] is True


def test_safe_subprocess_env_filters_tokens(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    tools = RepoWorkspaceTools(workspace_root=str(tmp_path), allowed_repos=["schmits/repo-sandbox-fixture"])

    env = tools.get_status.__globals__["_safe_subprocess_env"]()

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"