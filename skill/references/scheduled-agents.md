# Scheduled Agents (Cron)

No `max_runs` needed — cost is entirely predictable from the rrule.

## rrule Format

The `rrule` field accepts RFC 5545 recurrence rules. Use `DTSTART;TZID=` for timezone-aware schedules:

| Schedule | rrule |
|----------|-------|
| Daily at 8am ET | `DTSTART;TZID=America/New_York:20260101T080000`<br>`RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=0` |
| Every 30 minutes | `RRULE:FREQ=MINUTELY;INTERVAL=30` |
| Weekly on Monday at 9am ET | `DTSTART;TZID=America/New_York:20260101T090000`<br>`RRULE:FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0` |

Multi-line rrules (DTSTART + RRULE) use `|-` YAML syntax. All examples in this doc use this format.

## Example: Daily System Health Check

```yaml
---
title: Daily System Health Check
trigger: schedule
rrule: |-
  DTSTART;TZID=America/New_York:20260101T080000
  RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=0
notify_channel: discord/general
notify: always
active: true
---

Check the system health and report any issues.

Steps:
1. Check disk usage with `df -h`
2. Check memory usage with `free -h`
3. Check if critical services are running
4. Report a summary with any warnings (disk > 80%, memory > 90%)

If everything looks healthy, respond with a brief "all clear" summary.
```

## Example: Weekly Report

```yaml
---
title: Weekly Project Summary
trigger: schedule
rrule: |-
  DTSTART;TZID=America/New_York:20260101T090000
  RRULE:FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0
notify_channel: email
notify: always
---

Summarize the past week's activity:
1. Review git commits across all active repos
2. Check completed and open issues
3. Highlight blockers or stale PRs
4. Produce a concise summary suitable for a status update
```

## Lifecycle Limits

Three fields control how long an agent stays active. All three are optional and independent — use any combination.

| Field | Effect | Agent stays active? |
|-------|--------|---------------------|
| `rate_limit` | Per-window throttle (`"N/unit"`, e.g., `"10/hour"`). Excess dispatches are dropped. | Yes (warning only) |
| `max_runs` | Total dispatches per activation cycle before auto-disable. Count resets when re-enabled. | No — auto-disables |
| `expires_at` | ISO 8601 datetime. Agent auto-disables when this time is reached. | No — auto-disables |

### Re-enabling

- For `max_runs`: set `active: true`. The run count resets automatically on re-enable.
- For `expires_at`: update to a future datetime or remove the field, then set `active: true`.

### Example: Fire-Once Reminder

Use `max_runs: 1` for tasks that should fire exactly once:

```yaml
---
title: Push Release Branch
trigger: schedule
rrule: |-
  DTSTART;TZID=America/New_York:20260313T150000
  RRULE:FREQ=DAILY;BYHOUR=15;BYMINUTE=0
max_runs: 1
notify_channel: sms
notify: always
active: true
---

Remind Jack to push the release branch.
```

After dispatch, the dispatcher sets `active: false` in the file. The file stays on disk, disabled and ready to be reconfigured.

### Reusable Templates

Keep dormant `max_runs: 1` agent files as templates (e.g., `agents/reminders/reminder-1.md`). To schedule a reminder: update the prompt and rrule, set `active: true`, and let it fire. It auto-disables afterward, ready for the next use.

### Example: Time-Limited Agent

Use `expires_at` for agents that should stop after a deadline:

```yaml
---
title: Conference Monitor
trigger: webhook
event: twitter.mention
expires_at: "2026-03-15T18:00:00-04:00"
notify_channel: discord/general
notify: always
active: true
---

Monitor mentions during the conference and summarize each one.
```

### Caveats

- **Don't move a `max_runs` agent file after enabling it.** The dispatcher tracks the file path at scan time. If the file moves before the agent fires, the write-back fails (logged as an error) and the agent fires a second time from its new location before self-correcting.
- Lifecycle limits work with all trigger types (`schedule`, `webhook`, `both`).
- Auto-disable happens after retries are exhausted, so transient failures don't leave the agent in an ambiguous state.
