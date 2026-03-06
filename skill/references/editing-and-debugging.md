# Editing and Debugging Agents

## Editing an Agent

The dispatcher re-reads files every 60 seconds — no restart needed. For immediate pickup: `curl -X POST http://localhost:8790/reload`.

To disable without deleting: set `active: false` in frontmatter.

| Operation | What happens |
|-----------|-------------|
| Rename file | New agent ID (path-based). Old ID's state (last run, queue) becomes orphaned in the DB. The agent effectively becomes a new agent with no history. |
| Move to subfolder | Same as rename — agent ID changes. State doesn't follow. |
| Delete file | Agent stops being scheduled on next reload. DB state persists but is inert. Queue files (if any) remain on disk until manually cleaned. |
| Change `trigger` type | Supported — just edit the frontmatter. If switching from `both` to `schedule`, any pending queue is ignored (not drained). Drain first by triggering a manual run, or delete the queue file. |
| Convert Trigger to Inbox | Change `trigger: webhook` to `trigger: both`, add `rrule` and `defer_to_cron`. Add `{{ queue_file }}` to the prompt. Events will queue starting from the next webhook — no retroactive queueing. |

---

## Debugging an Agent

### Check Execution Logs

```bash
# Recent logs
tail -100 /dev/shm/zo-dispatcher.log

# Loki query for specific agent
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={filename="/dev/shm/zo-dispatcher.log"} |~ "<agent-title>"' \
  --data-urlencode "start=$(date -d '2 hours ago' +%s)000000000" \
  --data-urlencode "end=$(date +%s)000000000" \
  --data-urlencode "limit=50" | jq -r '.data.result[0].values[]? | .[1]'

# Webhook-specific logs
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={filename="/dev/shm/zo-dispatcher.log"} |~ "source.*<source-name>"' \
  --data-urlencode "start=$(date -d '1 hour ago' +%s)000000000" \
  --data-urlencode "limit=50" | jq -r '.data.result[0].values[]? | .[1]'

# All failed agents today
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={filename="/dev/shm/zo-dispatcher.log"} |~ "Agent failed"' \
  --data-urlencode "start=$(date -d 'today 00:00' +%s)000000000" \
  --data-urlencode "limit=50" | jq -r '.data.result[0].values[]? | .[1]'
```

### Check State

```bash
dispatcher-cli agent show <agent_id>
dispatcher-cli webhook list
dispatcher-cli agent list
```

### Force Cache Refresh

```bash
curl -X POST http://localhost:8790/reload
```

### Common Issues

| Symptom | Likely Cause |
|---------|-------------|
| Agent isn't firing for webhooks | Cache takes 60s. Use `/reload`. Check source is registered, signature verifies, event matches. |
| Webhook returns 200 but nothing dispatches | Check: (1) source registered? (2) signature verify? (3) event type match? (4) `max_runs` hit? |
| Builtin channel not delivering | Check `ZO_CLIENT_IDENTITY_TOKEN` in environment. Look for `MCP tool error` in logs. |
| Transform changes not taking effect | Scripts cached by mtime. `touch` the file to force reload. |
| Fires at wrong time | Check rrule timezone. Use `DTSTART;TZID=` for timezone-aware schedules. |
| No notification on success | `notify: errors` suppresses success notifications. |
| No notification at all | `notify_channel` not set — agent runs silently. |

---

### Debug Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/webhooks` | GET | List registered webhook sources |
| `/channels` | GET | List registered channels |
| `/agents` | GET | List all agents |
| `/agents/{agent_id}` | GET | Show agent details |
| `/reload` | POST | Force agent/config cache refresh |
| `/dispatch` | POST | Manually dispatch an agent |

### Check Service Health

```bash
curl http://localhost:8790/health
tail -50 /dev/shm/zo-dispatcher_err.log
```
