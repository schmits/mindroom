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
      # Optional plugin-local dedupe persisted below ctx.state_root by default.
      # dedupe_state_dir may be absolute or relative, but must resolve inside ctx.state_root.
      # The state file is bounded, atomically replaced, and process-local locked
      # so duplicate concurrent terminal hooks do not both notify.
      dedup_enabled: true
      dedupe_state_dir: .   # optional; defaults to ctx.state_root
      dedup_max_entries: 512
      # Optional dynamic scoping when decorator-level static scoping is not enough.
      agents: [mind]
      rooms: ["!room:example.org"]
      # Keep false unless payload recipients may receive response text.
      include_response_text: false
```

Hook timeouts are set to 1000 ms and normal MindRoom hook execution isolates plugin failures from response delivery. The plugin also catches Matrix notification-send failures and logs a warning.

## Dedupe state

When `dedup_enabled` is true, the plugin records terminal notification keys in `dedupe.json` under the plugin `ctx.state_root`, or under configured `dedupe_state_dir` when that directory resolves inside `ctx.state_root`. The file contains only minimized event identifiers and timestamps, never response text. Existing list-shaped dedupe files from the initial plugin release are still accepted and are rewritten to the structured bounded format on the next successful notification.

Dedupe state is written to runtime plugin state, not the plugin source directory. If an older deployment already has `dedupe.json` in this plugin directory, the plugin copies those minimized dedupe keys into the runtime state file on first use for backward compatibility and then continues using runtime state.

## Plugin-only limitations

Because this is intentionally plugin-only, it does not expose the exact internal session completion facts proposed in PR #15/#16, including:

- `run_id`
- full internal `session_id`
- `run_succeeded`
- `source_handled`
- post-`ResponseLifecycle.finalize` facts

Consumers should treat this as a best-effort terminal notification based on the public `message:after_response` and `message:cancelled` hook contexts.