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
