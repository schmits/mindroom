# Dynamic Workflow Promotion Plugin

Provides the `dynamic_workflow_promotion` toolkit with one plugin-scoped function, `promote_dynamic_workflow_spec`.

The tool promotes a pre-validated Dynamic Workflow spec only when the workflow spec bytes, validation artifact, approval evidence, target scope/ref, approver, expected hash, rollback policy, and reason all match. `dry_run` or `preflight` performs the same validation without persistence. Apply writes only plugin-private promotion records plus append-only audit records under the plugin state root.

Safety boundaries:

- no core runtime changes;
- no broad config writer and no default grants;
- local JSON artifact refs only, optionally constrained by `allowed_artifact_roots`;
- fails closed on spec substitution, approval replay, stale schema, ambiguous scope, self-promotion, expired/revoked/redacted approval evidence, forbidden workflow tool grants, rollback abuse, or audit write failure.

Rollback actions are represented in the typed promotion record. `restore_previous`, `tombstone`, and `delete` require an equal-or-stronger `authorization_ref` and are rejected if they reuse the existing promotion approval reference.