# zo-dispatcher

A general-purpose agent dispatcher for [Zo Computer](https://zo.computer) — from simple cron jobs to multi-source webhook inboxes, using markdown files and payload transform scripts.

## Features

**Scheduling**
- RFC 5545 recurrence rules for cron-like scheduling

**Webhooks**
- Signature verification and event filtering
- Dot-notation event matching (e.g., `todoist.item:added`)
- Multi-event agents: match multiple event patterns (cross-source) in a single agent
- Payload transform scripts for custom verification, pre-filtering, and token efficiency
- Batch processing via `defer_to_cron` — queue events and drain on schedule

**Notifications**
- Route output to Discord ([zo-discord](https://github.com/JackALaing/zo-discord)), SMS, email, or Telegram
- Per-agent notification levels: `always`, `errors`, or `never`
- Business hours queueing — notifications held until your configured window

**Multi-Backend**
- Dispatch to the Zo API (`backend: zo`) or a local [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance (`backend: hermes`) via [zo-hermes](https://github.com/JackALaing/zo-hermes)
- Set `default_backend` in `config/config.json` to choose which backend agents use when they omit `backend`
- Hermes agents support per-agent reasoning effort, iteration limits, memory/context toggles, and toolset restrictions

**Reliability & Cost Control**
- Built-in Zo API resilience: per-agent `timeout` and `retry_delays`, automatic retry on empty/failed responses, session pool recovery
- Webhook deduplication via event ID tracking
- Serial processing per webhook source — events processed in order
- Global concurrency control (`max_concurrent_dispatches`)
- Per-agent dispatch budgets (`max_runs` per time window)

**Management**
- Markdown files as source of truth — agent files can live in any directory (e.g., an Obsidian vault) with any subfolder structure
- Hot reloading of agents, config, and transforms without restarts
- Unified CLI for schedules, webhooks, and channels

## Dispatch Patterns

Every agent maps to one of four dispatch patterns:

| Pattern | Config | One-Liner |
|---------|--------|-----------|
| **Cron** | `trigger: schedule` | Runs on a clock, no event input |
| **Trigger** | `trigger: webhook` | Fires immediately per event |
| **Sentinel** | `trigger: both`, `defer_to_cron: false` | Fires immediately per event AND runs on schedule independently |
| **Inbox** | `trigger: both`, `defer_to_cron: skip_if_empty` or `always_run` | Queues events, drains on schedule |

See `skill/SKILL.md` for full pattern documentation with examples, decision tree, cost model, and anti-patterns.

## Requirements

- A [Zo Computer](https://zo.computer) account
- Python 3.10+

## Setup

### 1. Get a Zo API Key

1. Go to [Settings > Advanced](/?t=settings&s=advanced)
2. Create an Access Token in the **Access Tokens** area
3. Save it as a Secret called `DISPATCHER_ZO_API_KEY` in the **Secrets** area on the same page

### 2. Clone and Install

```bash
git clone https://github.com/JackALaing/zo-dispatcher.git
cd zo-dispatcher
pip install .
```

This makes the `dispatcher-cli` command available on PATH automatically.

### 3. Configure

```bash
cp config/config.example.json config/config.json
```

Edit `config/config.json`. At minimum, set `agents_dir` to wherever you want to store agent markdown files. See `skill/SKILL.md` → **Config Reference** for all fields.

### 4. Create Your Agents Directory

Create the directory you specified in `agents_dir`. Subdirectories are used for namespacing:

```bash
mkdir -p agents/schedules agents/webhooks
```

Agent IDs are derived from relative paths. A file at `agents/schedules/daily-summary.md` gets the ID `schedules/daily-summary`.

### 5. Register as a Zo Service

Register the dispatcher as a Zo service so it auto-starts and restarts on failure:

```
Register zo-dispatcher as a service with entrypoint start.sh in /path/to/zo-dispatcher
```

The `start.sh` script loads your Zo secrets and starts the dispatcher. The Zo service system handles auto-restart.

### 6. Install the Skill

Expose the repo skill in your Zo skills directory with a symlink so the installed skill stays in sync with the service repo:

```bash
ln -sfn "$(pwd)/skill" /home/workspace/Skills/zo-dispatcher
```

### 7. Route Zo to zo-dispatcher

Create a Zo rule so your AI assistant uses zo-dispatcher instead of Zo's built-in agents:

**Condition:** User asks to create, edit, delete, schedule, or manage an agent, scheduled cron, recurring task, cron job, automated scheduled task, webhook agent, or agent trigger

**Instruction:** Do NOT use create_agent, edit_agent, or delete_agent tools. Use zo-dispatcher instead. Read `Skills/zo-dispatcher/SKILL.md` for the full agent workflow.

## Documentation

Detailed documentation lives in the `skill/` directory:

- **`skill/SKILL.md`** — Dispatch patterns, agent file format, notification channels, cost control, writing your first agent, config reference
- **`skill/references/scheduled-agents.md`** — Cron agent setup and examples
- **`skill/references/webhook-agents.md`** — Webhook setup, Trigger/Sentinel/Inbox patterns with examples
- **`skill/references/transforms.md`** — Payload transform scripts: reshaping, event dropping, custom signature verification
- **`skill/references/editing-and-debugging.md`** — Editing agents, debug endpoints, logs, Loki queries, common issues
- **`skill/references/cli.md`** — CLI command reference

## Hermes Backend

Set `backend: hermes` in an agent file to run that agent through the local [zo-hermes](https://github.com/JackALaing/zo-hermes) bridge instead of Zo's `/zo/ask` endpoint.

If you want Hermes to be the default for new agents, set `"default_backend": "hermes"` in `config/config.json`. `config/config.example.json` keeps the safer default of `"zo"`.

What that unlocks:

- local Hermes execution for that agent
- per-agent Hermes controls: `reasoning`, `max_iterations`, `skip_memory`, `skip_context`, `tools`, `tools_deny`
- direct pairing with `notify_channel: discord/<channel-name>` when you want Hermes-backed work to land in a `zo-discord` thread
- reuse of the same agent file format, with the caveat that Zo `persona` frontmatter is currently meaningful only on the Zo backend. Hermes dispatch still accepts the field in the schema, but `zo-hermes` does not map it to a Hermes personality

Scope boundaries:

- This applies only to `zo-dispatcher` agents with `backend: hermes`.
- It does not affect native Zo agents.
- It does not make arbitrary webhook sources talk to Hermes unless they are routed through `zo-dispatcher` or another caller that hits `zo-hermes` directly.
- If you want Hermes output in Discord, you still need `zo-discord` for the Discord delivery layer.

## Architecture

```
External Service (GitHub, Todoist, etc.)
        │
        ▼
zo.space /api/webhook/:source   ← thin HTTPS proxy
        │
        ▼ POST http://localhost:8790/webhook/:source
        │
zo-dispatcher (aiohttp server + poll loop)
        │
        ├─ Webhook Registry (SQLite)
        │   └─ Lookup source → verify signature → check dedupe
        │
        ├─ Payload Transform (transforms/<source>.py)
        │   └─ Optional: reshape payload, drop events (return None), custom verify
        │
        ├─ Event Matching (dot-notation filtering, multi-event patterns)
        │   └─ todoist.item:added → match agents
        │
        ├─ Agent Files (<agents_dir>/*.md)
        │   └─ Scan for trigger match (webhook) or schedule due (rrule)
        │
        ├─ Template injection ({{ payload }}, {{ event_type }})
        │
        ├─ Backend dispatch (per-agent `backend` field or config `default_backend`)
        │   ├─ call_zo_ask()    ← Zo API with retry + session pool recovery
        │   └─ call_hermes()    ← Local Hermes Agent API (localhost:8788)
        │
        └─ Notification routing
                │
                ├─ Success: MCP direct call (SMS/email/Telegram)
                │          OR dispatcher POSTs to custom channel (Discord via zo-discord)
                └─ Errors:  dispatcher calls MCP directly
```

## Project Structure

```
zo-dispatcher/
├── zo_dispatcher/             # Python package
│   ├── __init__.py
│   ├── server.py              # Main service — poll loop, webhook server, dispatch engine
│   ├── agents.py              # Agent file parsing and rrule scheduling
│   ├── webhooks.py            # Signature verification, transforms, event matching
│   ├── channels.py            # Notification channel delivery (builtin + custom)
│   ├── cli.py                 # CLI tool
│   └── db.py                  # SQLite database — runs, webhooks, events, notifications
├── config/
│   ├── config.json            # Your config (gitignored)
│   └── config.example.json    # Config template
├── skill/
│   ├── SKILL.md                       # Patterns, file format, channels, cost control, first agent, config
│   └── references/
│       ├── scheduled-agents.md        # Cron agent setup and examples
│       ├── webhook-agents.md          # Webhook fundamentals + Trigger/Sentinel/Inbox patterns
│       ├── editing-and-debugging.md   # Editing agents, logs, Loki, common issues
│       ├── transforms.md             # Transform scripts with source-specific examples
│       └── cli.md                     # CLI command reference
├── tests/
│   ├── test_agents.py         # Agent parsing and scheduling tests
│   ├── test_webhooks.py       # Signature verification and event matching tests
│   ├── test_channels.py       # Channel delivery tests
│   └── test_db.py             # Database operation tests
├── transforms/                # Payload transform scripts (gitignored)
├── data/                      # SQLite database (gitignored)
├── start.sh                   # Service entrypoint (sources secrets, runs server)
├── pyproject.toml             # Package metadata and dependencies
├── LICENSE
└── README.md
```

## License

MIT
