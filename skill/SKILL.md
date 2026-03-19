---
name: zo-dispatcher
description: "Create, edit, debug, and manage zo-dispatcher agents. Covers dispatch patterns, agent file format, prompt authoring, webhook setup, channel configuration, scheduling, and debugging. Triggers on: 'create an agent', 'edit an agent', 'schedule a task', 'add a webhook', 'cron job', 'recurring task', 'debug agent', 'agent not running', 'scheduled task', 'zo-dispatcher'."
compatibility: Created for Zo Computer
metadata:
  author: JackALaing
---

# zo-dispatcher

zo-dispatcher is a custom agent dispatcher that replaces Zo's built-in agents. Do NOT use `create_agent`, `edit_agent`, or `delete_agent` when this skill is installed.

Key paths (relative to the zo-dispatcher install directory):

- Agent definitions: configured via `agents_dir` in `config/config.json`
- Config: `config/config.json`
- Provider blueprints: `config/providers.yaml` (auto-fills `webhook add` flags for known providers)
- State: SQLite database at path configured via `db_path`
- Logs: `/dev/shm/zo-dispatcher.log` (indexed by Loki)
- Transforms: configured via `transforms_dir`
- CLI: `dispatcher-cli` (available on PATH after `pip install .`)

The dispatcher reads agent files, dispatches to the Zo API or a local Hermes Agent on schedule or webhook triggers, and routes output to notification channels.

**Reference docs** (load only when needed):

- `references/scheduled-agents.md` — MANDATORY before creating any Cron agent
- `references/webhook-agents.md` — MANDATORY before creating any webhook, Sentinel, or Inbox agent
- `references/editing-and-debugging.md` — MANDATORY before debugging or modifying agent lifecycle
- `references/transforms.md` — MANDATORY before registering a webhook source (includes registration commands and copy-paste-ready transform scripts for common sources)
- `references/cli.md` — CLI command reference

---

## Dispatch Patterns

Every agent maps to one of four dispatch patterns. Each pattern corresponds to a distinct configuration shape.

| Pattern | Config | One-Liner |
|---------|--------|-----------|
| **Cron** | `trigger: schedule` | Runs on a clock, no event input |
| **Trigger** | `trigger: webhook` | Fires immediately per event |
| **Sentinel** | `trigger: both`, `defer_to_cron: false` | Fires immediately per event AND runs on schedule independently |
| **Inbox** | `trigger: both`, `defer_to_cron: skip_if_empty` or `always_run` | Queues events, drains on schedule |

Any pattern can be combined with lifecycle limits (`max_runs`, `rate_limit`, `expires_at`) to control how long an agent stays active. For example, `max_runs: 1` makes any agent fire once then auto-disable.

For Cron details, see `references/scheduled-agents.md`. For Trigger, Sentinel, and Inbox details, see `references/webhook-agents.md`.

### Choosing a Pattern

```
Is this a one-time task (reminder, deferred action, single send)?
├── Yes → Add `max_runs: 1` to any pattern below
└── No →

Does the agent need external events at all?
├── No → Cron (trigger: schedule)
└── Yes →
    Is the event urgent (needs response in minutes)?
    ├── Yes → Trigger (trigger: webhook)
    │         Also need periodic independent work? → Sentinel (trigger: both, defer_to_cron: false)
    └── No → Inbox (trigger: both)
              Does the agent have work beyond processing events?
              ├── No → defer_to_cron: skip_if_empty
              └── Yes → defer_to_cron: always_run
```

### Cost Mental Model

| Pattern | Cost Driver | Typical Range |
|---------|-------------|---------------|
| Cron | 1 LLM call per scheduled tick | Predictable, based on frequency |
| Trigger | 1 LLM call per event, capped by `rate_limit` | Depends on event volume |
| Sentinel | 1 LLM call per event + 1 per scheduled tick | Highest (reactive + proactive) |
| Inbox `skip_if_empty` (heartbeat, 30-min) | ~48 LLM calls/day max, only when events exist | Low-Medium |
| Inbox `skip_if_empty` (daily) | 1 LLM call/day, only when events exist | Very Low |
| Inbox `always_run` (daily) | 1 LLM call/day, always | Low (predictable like Cron, but also drains events) |

Inbox is dramatically cheaper than Trigger or Sentinel for high-volume sources because it amortizes the baseline token overhead (system prompt, tool loading, context bootstrapping) across all events in a batch.

### Anti-Patterns

1. **Trigger on high-volume sources.** If a source fires 100+ events/day, use Inbox instead.
2. **Sentinel or Trigger without `rate_limit`.** Any agent with webhook triggers can run away without a budget cap.
3. **Inbox with too-short drain interval.** Draining every 1-2 minutes defeats the purpose of batching. Use 15-30 min minimum.
4. **Single agent trying to do both urgent filtering and batch processing.** Use two agents: Trigger for urgency, Inbox for batching. They can listen to the same events.
5. **Forgetting transform scripts.** Transforms strip payloads to essentials and can drop events entirely by returning `None` — the cheapest possible filter.
6. **Inbox prompt without `{{ queue_file }}`.** The agent has no way to read queued events. The parser warns, but the agent will fire and produce a generic response with no event data.
7. **Prompts that assume memory across runs.** Each dispatch is a fresh `/zo/ask` call. The agent doesn't remember previous runs. If it needs continuity, the prompt must include explicit file reads for state.
8. **Agents writing to the same file without coordination.** Two agents that both write to the same output file on overlapping schedules will clobber each other. Use distinct output files or make one agent depend on the other's output.
9. **Transform scripts doing LLM-level reasoning.** Transforms are cheap deterministic filters. If your transform needs to "understand" the payload, that work belongs in the agent prompt. Keep transforms to field extraction, keyword matching, and sender filtering.
10. **Prompts without context bootstrapping.** The agent wakes up blank. If it needs workspace context (files, config, recent state), the prompt must explicitly instruct it to read those files.
11. **Moving a `max_runs` agent file after enabling it.** The dispatcher caches the file path at scan time. Move before it fires = write-back fails + one extra firing.

---

## Agent File Format

Each agent is a markdown file with YAML frontmatter and a markdown body (the prompt).

### Location & Organization

Files live under `agents_dir`, optionally organized in subfolders. Any directory structure works — the example below is just one convention:

```
agents/
├── schedules/
│   ├── daily-summary.md
│   └── weekly-report.md
└── webhooks/
    ├── github-issue.md
    └── readwise-daily.md
```

Agent IDs are path-based: `schedules/daily-summary`, `webhooks/github-issue`.

### Frontmatter Fields

| Field             | Relevant Triggers  | Required | Default        | Description                                                                       |
| ----------------- | ------------------ | -------- | -------------- | --------------------------------------------------------------------------------- |
| `title`           | all                | yes      | —              | Display name                                                                      |
| `trigger`         | all                | yes      | —              | `schedule`, `webhook`, or `both`                                                  |
| `rrule`           | `schedule`, `both` | yes      | —              | RFC 5545 recurrence rule                                                          |
| `event`           | `webhook`, `both`  | yes      | —              | Webhook event filter: string or list of strings (dot-notation). List = match any. |
| `persona`         | all                | no       | —              | Persona ID (`per_XXX`).                                                           |
| `model`           | all                | no       | config default | Model ID                                                                          |
| `notify_channel`  | all                | no       | —              | `<channel>` or `<channel>/<sub_channel>`                                          |
| `notify`          | all                | no       | `errors`       | `always`, `errors`, `never`                                                       |
| `timeout`         | all                | no       | config default | Seconds for `/zo/ask` timeout                                                     |
| `retry_delays`    | all                | no       | config default | List of retry delay seconds                                                       |
| `rate_limit`      | all                | no       | unlimited      | Per-window throttle: `"N/unit"` where unit is `minute`, `hour`, or `day`. Excess dispatches are dropped (agent stays active). |
| `max_runs`        | all                | no       | unlimited      | Total dispatches before auto-disable. Count resets when the agent is re-enabled.   |
| `expires_at`      | all                | no       | —              | ISO 8601 datetime. Agent auto-disables when this time is reached. Naive datetimes treated as UTC. |
| `defer_to_cron`   | `both` only        | no       | `false`        | `false`, `skip_if_empty`, or `always_run`. Requires `trigger: both`.              |
| `backend`         | all                | no       | `zo`           | `zo` (Zo API) or `hermes` (local Hermes Agent at localhost:8788)                  |
| `active`          | all                | no       | `true`         | Set false to disable                                                              |

**Hermes-only fields** (ignored when `backend: zo`):

| Field             | Required | Default  | Description                                                                       |
| ----------------- | -------- | -------- | --------------------------------------------------------------------------------- |
| `reasoning`       | no       | —        | Reasoning effort: `off`, `low`, `medium`, `high`                                  |
| `max_iterations`  | no       | —        | Max tool-use iterations per turn                                                  |
| `skip_memory`     | no       | `false`  | Skip loading Hermes persistent memory                                             |
| `skip_context`    | no       | `false`  | Skip loading context files (AGENTS.md, .hermes.md, etc.)                          |
| `tools`           | no       | —        | Enabled toolset whitelist (e.g., `[web, file, terminal]`). Mutually exclusive with `tools_deny`. |
| `tools_deny`      | no       | —        | Disabled toolset blacklist (e.g., `[browser, rl]`). Mutually exclusive with `tools`. |

### Template Variables

| Variable | Available | Description |
|----------|-----------|-------------|
| `{{ payload }}` | webhook | JSON payload, pretty-printed |
| `{{ event_type }}` | webhook | Extracted event type |
| `{{ date }}` | all | Current date (e.g., "2026-03-04") |
| `{{ timestamp }}` | all | Current ISO 8601 timestamp |
| `{{ agent_id }}` | all | Namespaced ID (e.g., "schedules/daily-summary") |
| `{{ queue_file }}` | defer_to_cron | Snapshot JSONL path when events exist, or `"No events queued."` when empty (`always_run`) |

### Event Matching (Dot-Notation)

| Agent `event` | Incoming source + event_type | Match? |
|---------------|------------------------------|--------|
| `github` | github / any event | Yes (source-level) |
| `github.opened` | github / `opened` | Yes (exact) |
| `github.open` | github / `opened` | No (not a prefix) |
| `todoist.item:added` | todoist / `item:added` | Yes (exact) |

Multiple agents can listen to different events from the same source. The `event` field accepts a YAML list for multi-event matching (logical OR). Patterns can span multiple webhook sources for cross-source batching.

---

## Notification Channels

### `notify_channel` Format

- `sms` — SMS to the user
- `email` — Email to the user
- `telegram` — Telegram to the user
- `discord/<channel-name>` — Discord, routed to the named channel in your server (e.g., `discord/general`, `discord/alerts`) - requires zo-discord
- Omit for silent agents (no notifications)

### Custom Channels

Configured in `config/config.json`:

```json
{
  "channels": {
    "discord": {
      "url": "http://localhost:8787/notify"
    }
  }
}
```

Custom channels receive a POST with `{"title": "...", "content": "...", "conversation_id": "..."}`. For Discord, the sub-channel is passed as `channel_name` when using `channel/sub_channel` notation.

### Builtin Channels

SMS, email, and Telegram need no config. Set `notify_channel: sms` (or `email`, `telegram`) in the agent frontmatter.

**Advisory:** SMS and Telegram are single-threaded — all agent output lands in one conversation. Use Discord for multi-agent notifications. SMS/Telegram work best for single-agent alerts.

### Notification Levels

- `notify: always` — notify on success, failure, and lifecycle events (auto-disable from `max_runs`/`expires_at`). If `notify_channel` is not set, lifecycle notifications fall back to `system_notification_channel` so they are never silently lost.
- `notify: errors` — only on failure (default). Lifecycle auto-disables are silent (expected behavior, not errors).
- `notify: never` — no notifications at all, including lifecycle events

---

## Concurrency & Cost Control

Three-layer throttling model:

1. **Per-source serialization** — Events from the same webhook source dispatch sequentially. Cross-source events run in parallel.

2. **Global concurrency** (`max_concurrent_dispatches` in config) — Caps total simultaneous `/zo/ask` calls across **webhook agents only**. Scheduled agents bypass the semaphore. Default: 5.

3. **Per-agent lifecycle limits** — Three independent controls:
   - `rate_limit` (`"N/unit"`) — Per-window throttle. Excess dispatches are dropped with a warning. Agent stays active. **Strongly recommended for webhook and dual-trigger agents.** For `trigger: both`, the budget counts all runs regardless of trigger source.
   - `max_runs` — Lifetime dispatch cap per activation cycle. Agent auto-disables when reached. Count resets when re-enabled (set `active: true`).
   - `expires_at` — Absolute expiry. Agent auto-disables when the time is reached.

---

## Writing Your First Agent

Create a file in your agents directory, e.g., `agents/webhooks/readwise-daily.md`:

```yaml
---
title: Readwise Daily Digest
trigger: both
event: readwise.reader.any_document.created
defer_to_cron: skip_if_empty
rrule: |-
  DTSTART;TZID=America/New_York:20260101T090000
  RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0
notify_channel: sms
notify: always
active: true
---

The following articles were saved to Readwise in the last 24 hours:

{{ queue_file }}

For each article:
1. Write a 2-sentence summary
2. Assign 1-3 tags based on topic
3. Note connections to previously saved articles on similar themes

Produce a brief daily reading digest with the summaries, tags, and any cross-references.
```

This is an **Inbox** agent: webhook events from Readwise are silently queued, then drained once daily at 9am. If nothing was saved that day, the agent doesn't run (`skip_if_empty`).

Test it manually:

```bash
dispatcher-cli agent run webhooks/readwise-daily
```

---

## Config Reference

`config/config.json`:

| Key                           | Default                   | Description                                                                           |
| ----------------------------- | ------------------------- | ------------------------------------------------------------------------------------- |
| `agents_dir`                  | `./agents`                | Path to agent markdown files                                                          |
| `db_path`                     | `./data/dispatcher.db`    | Path to SQLite database                                                               |
| `zo_api_url`                  | `https://api.zo.computer` | Zo API endpoint                                                                       |
| `default_model`               | —                         | Default model ID for agents without `model` (user must set)                           |
| `poll_interval_seconds`       | `60`                      | How often to check for due scheduled agents                                           |
| `transforms_dir`              | `./transforms`            | Path to transform scripts directory                                                   |
| `webhook_port`                | `8790`                    | HTTP server port                                                                      |
| `max_concurrent_dispatches`   | `5`                       | Global cap on simultaneous `/zo/ask` calls                                            |
| `jitter_max_seconds`          | `0`                       | Deterministic dispatch jitter for scheduled agents. Each agent sleeps `hash(id) % jitter_max_seconds` before dispatch. Spreads simultaneous due agents across a window. `0` = disabled. Keep below the agent's cron frequency to avoid skips. |
| `zo_ask_timeout_seconds`      | `1800`                    | Default timeout for `/zo/ask` calls                                                   |
| `empty_response_retry_delays` | `[15, 30, 60]`            | Retry delays for empty/failed responses                                               |
| `session_pool_retry_delays`   | `[15, 30, 60, 120]`       | Retry delays for session pool errors                                                  |
| `notification_hours`          | `{"start": 9, "end": 21}` | Business hours window for all notifications. Set `{"start": 0, "end": 24}` to disable |
| `notification_timezone`       | `America/New_York`        | Timezone for business hours                                                           |
| `webhook_rate_limit`          | `60`                      | Max webhook requests per source per 60-second window                                  |
| `dedupe_hours`                | `24`                      | How long to remember webhook event IDs                                                |
| `system_notification_channel` | `sms`                     | Channel for system-level alerts                                                       |
| `channels`                    | `{}`                      | Custom channel configurations                                                         |

---

## Secret Rotation

Zero-downtime webhook secret rotation via CLI. The `secret_env` field supports comma-separated env var names — verification tries each until one matches.

```bash
dispatcher-cli webhook rotate <source>       # Start: copies secret to _OLD, accepts both
dispatcher-cli webhook rotate-cleanup <source> # Finish: reverts to primary, removes _OLD
```

Between `rotate` and `rotate-cleanup`, update the primary env var in Settings > Advanced with the new secret value, then update the secret at the provider. See `references/cli.md` for the full workflow.
