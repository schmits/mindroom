# Session Completion Notifier

Plugin-only terminal response notifications for MindRoom sessions.

This plugin uses existing public hooks only:

- `message:after_response` (`AfterResponseContext`) for completed visible responses.
- `message:cancelled` (`CancelledResponseContext`) for cancelled or error terminal outcomes.

It does **not** modify MindRoom core runtime files or add a new core hook/seam.

## Payload

The plugin emits a minimized JSON-safe payload:

```json
{
  "status": "completed | cancelled | error",
  "agent": "agent_name",
  "room": {
    "id": "!room:example.org",
    "thread_id": "$resolved_thread_root_or_null",
    "source_thread_id": "$source_thread_or_null",
    "reply_to_event_id": "$reply_event_or_null"
  },
  "source_event_id": "$incoming_event",
  "response_event_id": "$response_event_when_available_or_null",
  "correlation_id": "hook_correlation_id",
  "response_kind": "ai | team | ...",
  "delivery": {
    "kind": "sent | edited | cancelled | failed | ...",
    "failure_reason": "reason_or_null"
  }
}
```

Response text is excluded by default. Set `include_response_text: true` only if the notification destination is allowed to receive response content.

## Configuration

Add the plugin to `config.yaml` with optional settings:

```yaml
plugins:
  - path: ./plugins/session-completion-notifier
    settings:
      enabled: true
      # Log the payload through the MindRoom logger. Defaults to false.
      log_payload: false
      # Optional Matrix notification sink. If omitted, the plugin only logs.
      notify_room_id: "!ops:example.org"
      notify_thread_id: "$thread"   # optional
      # Or send back to the source room/thread instead of notify_room_id.
      send_to_source_room: false
      # Optional plugin-local dedupe persisted below ctx.state_root.
      # The state file is bounded, atomically replaced, and process-local locked
      # so duplicate concurrent terminal hooks do not both notify.
      dedup_enabled: true
      dedup_max_entries: 512
      # Optional parent-ledger bridge. This writes minimized, bounded Matrix
      # room state with public hook helpers only; no config apply or grants.
      parent_ledger_enabled: false
      parent_ledger_room_id: "!parent:example.org"
      parent_ledger_state_key: mind     # defaults to the responding agent
      parent_ledger_state_event_type: mindroom.session_completion.ledger
      parent_ledger_max_entries: 256
      # Or write the ledger into the source room instead of parent_ledger_room_id.
      parent_ledger_to_source_room: false
      # Optional dynamic scoping when decorator-level static scoping is not enough.
      agents: [mind]
      rooms: ["!room:example.org"]
      # Keep false unless payload recipients may receive response text.
      include_response_text: false
```

Hook timeouts are set to 1000 ms and normal MindRoom hook execution isolates plugin failures from response delivery. The plugin also catches Matrix notification-send failures and logs a warning.

## Parent ledger bridge

When `parent_ledger_enabled` is true, the plugin updates one Matrix state event using the public `query_room_state` and `put_room_state` hook helpers. The default event type is `mindroom.session_completion.ledger`; the default state key is the responding agent name. The content is bounded and versioned:

```json
{
  "version": 1,
  "updated_at": 4567.0,
  "completions": [
    {
      "key": "completed|corr|$source|$response|",
      "status": "completed",
      "agent": "mind",
      "room_id": "!room:example.org",
      "thread_id": "$thread_or_null",
      "source_event_id": "$source",
      "response_event_id": "$response_or_null",
      "correlation_id": "corr",
      "response_kind": "ai",
      "delivery_kind": "sent",
      "failure_reason": null,
      "first_seen_at": 4567.0,
      "updated_at": 4567.0
    }
  ]
}
```

The bridge never includes `response_text`, even when notification payloads opt in to response text. Repeated terminal keys replace the existing ledger entry rather than appending a duplicate. A process-local lock serializes read/modify/write for the same `(room_id, event_type, state_key)`, and failures to read or write the optional parent ledger are warning-only so response delivery and ordinary notifications remain isolated.

## Dedupe state

When `dedup_enabled` is true, the plugin records terminal notification keys in `dedupe.json` under the plugin `ctx.state_root`. The file contains only minimized event identifiers and timestamps, never response text. Existing list-shaped dedupe files from the initial plugin release are still accepted and are rewritten to the structured bounded format on the next successful notification.

Dedupe state is written to runtime plugin state, not the plugin source directory.

## Plugin-only limitations

Because this is intentionally plugin-only, it does not expose the exact internal session completion facts proposed in PR #15/#16, including:

- `run_id`
- full internal `session_id`
- `run_succeeded`
- `source_handled`
- post-`ResponseLifecycle.finalize` facts

Consumers should treat this as a best-effort terminal notification and parent-ledger projection based on the public `message:after_response` and `message:cancelled` hook contexts.