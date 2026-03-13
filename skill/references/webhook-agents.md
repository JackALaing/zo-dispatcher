# Webhook Agents

## Setup

### Step 1: Register the Webhook Source

For known providers (listed in `dispatcher-cli webhook providers`), registration is zero-flag:

```bash
dispatcher-cli webhook add github
```

This auto-fills all configuration from `config/providers.yaml`. CLI flags override blueprint values for custom cases. For unknown providers, specify flags manually:

```bash
dispatcher-cli webhook add <source> \
  --secret-env <ENV_VAR_NAME> \
  --signature-header <header-name> \
  [--signature-algo hmac-sha256-hex] \
  [--signature-prefix "sha256="] \
  [--event-type-path '$.type'] \
  [--event-id-path '$.id'] \
  [--transform-script <source>.py]
```

**Security:** `--secret-env` is required unless `--allow-unsigned` is explicitly passed. Always prefer signed webhooks.

### Step 2: Save the Webhook Secret

Direct the user to [Settings > Advanced](/?t=settings&s=advanced) to add the webhook secret as an environment variable (the name must match `--secret-env`).

### Step 3: Create the Agent File

Create a markdown file in the agents directory. Choose the pattern (Trigger, Sentinel, or Inbox) based on the [decision tree](../SKILL.md#choosing-a-pattern) in SKILL.md. Set the frontmatter fields accordingly and write the prompt body.

**Always set `max_runs` for webhook and dual-trigger agents** to control LLM token costs.

For Inbox agents, the prompt must include `{{ queue_file }}` (the parser warns if missing).

### Step 4: Configure the External Service

Tell the user to configure the external service to POST webhooks to:
```
https://<your-handle>.zo.space/api/webhook/<source>
```

### Step 5: Test

```bash
dispatcher-cli webhook test <source> --payload '{"type": "test.event", "id": "test_001"}'
```

This calls `/reload` first, so new agent files are picked up immediately.

---

## Transforms

Every webhook agent benefits from a transform script. Transforms run before the LLM — they're cheap, deterministic Python that reduces cost and noise across all patterns:

- **Reshape payloads** — extract only the fields the agent needs, cutting token waste. For Inbox agents, leaner payloads also mean a smaller JSONL queue at drain time.
- **Drop events** — return `None` to silently discard irrelevant events. The webhook is acknowledged (HTTP 200) but no agent fires and no LLM call is made.
- **Custom signature verification** — implement non-standard verification when the source doesn't use standard HMAC headers.

Use `--transform-script <source>.py` when registering the webhook source. See `references/transforms.md` for the interface and examples.

---

## Trigger Pattern

Fires immediately on every matching webhook event. Use for high-urgency, low-volume events.

**Always set `max_runs`.** Each event is a separate LLM call. Without a budget cap, a burst of events can run up costs fast.

### Example: GitHub Issue Alert

```yaml
---
title: GitHub Issue Alert
trigger: webhook
event:
  - github.opened
  - github.reopened
notify_channel: discord/general
notify: always
max_runs: 50
---

A new issue was filed or reopened.

{{ payload }}

Summarize: which repo, who filed it, what the issue is about. Flag if it looks urgent.
```

---

## Sentinel Pattern

Use when you need both immediate reaction to events and independent periodic work. Uses `trigger: both` with `defer_to_cron: false` (the default).

**Always set `max_runs`.** The budget counts both scheduled + webhook runs combined.

### Example: GitHub PR Sentinel

```yaml
---
title: GitHub PR Sentinel
trigger: both
rrule: |-
  DTSTART;TZID=America/New_York:20260101T090000
  RRULE:FREQ=DAILY;BYHOUR=9,17;BYMINUTE=0
event: github.opened
notify_channel: discord/general
notify: always
max_runs: 50
max_runs_window: 86400
---

If triggered by a webhook, evaluate the incoming PR:

{{ payload }}

Summarize: which repo, who opened it, what it changes. Flag if it touches critical paths
(auth, payments, infra) or has a large diff.

On every scheduled run (2x daily), regardless of whether a webhook triggered it:
1. Check for PRs that have been open for more than 48 hours without review
2. Find PRs with failing CI checks
3. Look for stale PRs that can be closed
4. Report anything that needs attention
```

---

## Inbox Pattern

Queues webhook events as they arrive (no LLM cost). On each scheduled tick, drains the queue and processes all accumulated events in a single LLM call.

| Interval  | Flavor             | Use Case                                                 |
| --------- | ------------------ | -------------------------------------------------------- |
| 15-30 min | **Heartbeat**      | Continuous awareness. Use with pre-filtering transforms. |
| Daily     | **Daily digest**   | Morning briefing, inbox triage, activity summary.        |
| Weekly    | **Weekly roundup** | Sprint reviews, retros, trend analysis.                  |

### `defer_to_cron` Setting

| `defer_to_cron` | Events | Schedule | Cost |
|-----------------|--------|----------|------|
| `false` (Sentinel) | Each event fires an immediate LLM call | Schedule also fires independently | Higher (per-event + per-tick) |
| `skip_if_empty` (Inbox) | Events queue silently (no LLM cost) | Drains the queue in one LLM call; **skips entirely if queue is empty** | Lowest (nothing when quiet) |
| `always_run` (Inbox) | Events queue silently (no LLM cost) | Drains the queue, then does additional work; **always runs even if queue is empty** | Low (one call per tick regardless) |

`skip_if_empty` is the pure Inbox — it only fires when there's work. `always_run` is for agents that have routine tasks beyond event processing that should run on every tick regardless.

### Example: Inbox with `skip_if_empty`

```yaml
---
title: Todoist Inbox Triage
trigger: both
rrule: |-
  DTSTART;TZID=America/New_York:20260101T060000
  RRULE:FREQ=DAILY;BYHOUR=6;BYMINUTE=0
event:
  - todoist.item:added
  - todoist.item:updated
defer_to_cron: skip_if_empty
notify_channel: discord/todoist
notify: always
---

Read all queued events from {{ queue_file }}.

For each new or updated task:
1. Classify: actionable, reference, someday/maybe, or delegate
2. Suggest a project, priority, and due date
3. If the task is vague, suggest a clearer next action

Output a triage report.
```

### Example: Inbox with `always_run`

Use when the agent has routine work beyond processing events:

```yaml
---
title: Todoist Daily Planner
trigger: both
rrule: |-
  DTSTART;TZID=America/New_York:20260101T060000
  RRULE:FREQ=DAILY;BYHOUR=6;BYMINUTE=0
event:
  - todoist.item:added
  - todoist.item:updated
defer_to_cron: always_run
notify_channel: discord/todoist
notify: always
---

## Step 1: Triage new tasks (drain)

Read queued events from {{ queue_file }}.

For each new or updated task:
1. Classify: actionable, reference, someday/maybe, or delegate
2. Suggest a project, priority, and due date

## Step 2: Plan the day

Regardless of whether new tasks arrived:
1. Check Todoist for tasks due today and overdue
2. Check the calendar for today's meetings
3. Propose a prioritized plan for the day

## Step 3: Report

Combined triage + daily plan.
```

### Examples of Routine Work Beyond Event Processing

| Use Case | Drain Step | Routine Work |
|----------|-----------|-----------------|
| Readwise Daily | Summarize new saved articles | Search library for older articles related to today's themes |
| Todoist Daily Planner | Triage new/updated tasks | Review due tasks, check calendar, plan the day |
| GitHub Project Pulse | Digest new issues, PRs, commits | Check CI status, find stale PRs, produce health score |
| Linear Sprint Health | Digest issues and comments | Check velocity, blockers, scope creep, unassigned work |

### Example: Cross-Source Digest

The `event` field accepts patterns from different webhook sources, enabling a single Inbox agent to batch events across sources:

```yaml
---
title: Daily Activity Digest
trigger: both
rrule: RRULE:FREQ=DAILY;BYHOUR=18;BYMINUTE=0
event:
  - github.push
  - github.pull_request
  - linear.issue
  - todoist.item
defer_to_cron: skip_if_empty
notify_channel: discord/general
notify: always
---

Read all queued events from {{ queue_file }}. Each line is a JSON object with
`event_type` (e.g., "push", "pull_request.opened", "issue.created") and `payload`.

Group events by source, summarize activity, and highlight anything that needs attention.
```

### How the Queue Works

Webhook events are appended to a JSONL queue file. On the next scheduled tick, the dispatcher checks the queue. If it has events, they are atomically snapshotted and `{{ queue_file }}` resolves to the snapshot path. If the queue is empty and the mode is `always_run`, `{{ queue_file }}` resolves to `"No events queued."`. On success, the snapshot is deleted. On failure, it's preserved for retry. Events arriving during a run go to a fresh queue. Deferred events do not count against `max_runs`.

Each JSONL line is: `{"event_type": "todoist.item.added", "payload": {...}, "received_at": "..."}`.
