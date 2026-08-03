# repo_workspace sandbox-only promotion gate threat model

`repo_workspace` is an ephemeral repository workspace substrate for sandbox-only source materialization, bounded file inspection/editing, status/diff generation, patch export, and explicit handoff descriptors. It is not a general automation or deployment tool.

## Assets and trust boundaries

- Workspace state is confined to the configured `workspace_root/<workspace_id>/repo` directory.
- Local source materialization is permitted only when `source_path` resolves under an explicit `allowed_source_roots` allowlist.
- Repository identity is constrained by explicit `allowed_repos` and `denied_repos` allowlists before any workspace is created.
- Workspace metadata records provenance and audit fields for materialization method, source boundary enforcement, write-confirmation policy, and non-execution/non-network behavior.

## In-scope validation

Positive gate coverage should verify:

- allowed `source_path` materialization from an allowlisted source root;
- file listing and line-numbered text reads inside the confined repo workspace;
- confirmed writes only inside the workspace repo;
- status, diff, and patch artifact export for copied local git checkouts;
- provenance and audit fields in workspace metadata.

Negative gate coverage should verify rejection of:

- path traversal and symlink escapes;
- direct reads of `.git` internals;
- unauthorized repositories and denied repositories;
- `source_path` outside configured `allowed_source_roots`;
- missing or invalid provenance/audit metadata;
- metadata `repo_dir` or source path boundary violations.

## Non-goals and explicit exclusions

`repo_workspace` must not provide or perform:

- shell execution or arbitrary command execution;
- package installation or dependency management;
- arbitrary network clone/fetch/pull operations;
- GitHub writes, PR creation, branch pushes, release uploads, or issue writes;
- ambient filesystem reads/writes outside the workspace repo and artifact directory;
- secret access, credential forwarding, token prompts, or ambient Git credential helpers.

Execution requests are represented only as handoff descriptors for a separate coding sandbox that must enforce its own policy, timeout, output capture, and secret isolation. Network materialization must be handled by an explicitly approved materialization tool, not by `repo_workspace`.
