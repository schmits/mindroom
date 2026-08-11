# repo_workspace plugin

This plugin packages the `repo_workspace` tool for MindRoom runtimes where the tool is not already registered in the base/default Docker image.

## Purpose

`repo_workspace` provides an ephemeral, repository-scoped file/diff substrate for safe code materialization and patch preparation. It is intentionally **not** a shell, package manager, GitHub publishing tool, or secrets transport.

## Runtime compatibility

The plugin assumes the runtime includes the current MindRoom plugin loader and tool metadata registration system.

It is intended only for deployments where the built-in `repo_workspace` tool is absent. If the runtime already registers a built-in `repo_workspace`, this plugin should fail closed via the existing duplicate tool metadata registration behavior instead of silently overriding the built-in tool.

## Files

- `mindroom.plugin.json` declares the plugin and its tool module.
- `tools.py` registers the `repo_workspace` tool metadata.
- `repo_workspace_impl.py` contains the toolkit implementation adapted from the built-in `src/mindroom/custom_tools/repo_workspace.py` implementation.
- `artifact_lease_links.py` packages the plugin-scoped metadata-only link model for durable handoff artifact ↔ workspace lease records.

## Safety boundaries

The plugin preserves the built-in tool boundaries:

- no arbitrary command execution;
- no clone/fetch/network materialization in the MVP;
- no GitHub writes;
- no ambient secrets passed to subprocesses;
- file operations confined to a workspace `repo/` directory;
- mutating workspace operations require `confirm_write=True`;
- execution is represented only as a controlled `coding_sandbox` handoff descriptor.

## Rollout notes

Use this plugin only as external plugin packaging for runtimes that need `repo_workspace` without rebuilding the default Docker image. Do not grant it to production `github_dev` agents unless the runtime boundary and agent policy have been reviewed separately.