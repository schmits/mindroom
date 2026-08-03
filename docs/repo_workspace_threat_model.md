# repo_workspace `source_path` identity boundary threat model

`repo_workspace` is an ephemeral repository workspace substrate for sandbox-only source materialization, bounded file inspection/editing, status/diff generation, patch export, and explicit handoff descriptors. It is not a general automation runner and it does not authenticate to GitHub.

## Boundary under review

`create_workspace(repo="owner/name", source_path="...")` may copy bytes from a pre-existing local checkout. The security boundary is the requested repository identity, not merely the filesystem path. A caller must not be able to request one allowed repository and smuggle bytes from another repository just because both paths live under an allowed source root.

This boundary applies to both the built-in tool implementation and the distributable plugin copy.

## Threats

- **Repository identity confusion**: an agent requests `schmits/allowed-repo` while passing `source_path` for `schmits/other-repo`, causing later reviewers or GitHub-preservation steps to trust the wrong source.
- **Path-only provenance spoofing**: a path under an allowed root is accepted without verifying Git origin or exact configured path binding.
- **Tampered workspace metadata**: persisted `metadata.json` is edited after creation so follow-up `workspace_info`, diff, export, or handoff operations appear to target a different repository than the copied source actually represented.
- **Origin spoofing via local path names**: a checkout path name matches the requested repository even though its configured Git remote points at another owner/repo.

## Required enforcement

When `source_path` is used, `repo_workspace` must prove one of the following before copying bytes:

1. The local source has a verifiable Git repository identity derived from a sanitized local `git remote get-url origin` result, and that identity exactly matches the requested `repo`; or
2. The local source has no origin, but the configured `allowed_source_roots` entry is an exact path binding for the requested repo and the source path is that exact root.

Owner-scoped wildcard repository grants such as `schmits/*` authorize *which requested repo names* may be used; they do not authorize copying arbitrary sibling repository bytes. The source checkout still has to match the requested repo identity.

## Runtime invariants

- Store the requested repository identity and the source repository identity in workspace metadata when a local source is materialized.
- Reject metadata where `source_path` was used but identity-boundary metadata is absent.
- Revalidate on `workspace_info` that recorded `source_repo_identity` still matches the workspace `repo`.
- Revalidate on `workspace_info` that the recorded source path remains inside the recorded allowed source roots.
- Reject tampered metadata before emitting handoff or provenance information that another agent may trust.
- Keep Git subprocess execution sanitized: no repo-local config execution, no external diff/textconv execution, and no ambient token environment.

## Non-goals

- `repo_workspace` still does not clone, fetch, push, install packages, execute arbitrary commands, or access GitHub credentials.
- This fix does not broaden any agent grants, repository allowlists, room access, runtime config, or GitHub permissions.