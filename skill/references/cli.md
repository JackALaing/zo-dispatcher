# CLI Reference

`dispatcher-cli` is available on PATH after `pip install .` from the zo-dispatcher directory.

---

## Webhook Commands

```bash
dispatcher-cli webhook add <source> \
  [--secret-env ENV_VAR] \
  [--signature-header HEADER] \
  [--signature-algo hmac-sha256-hex|hmac-sha256-base64|hmac-sha1-hex|custom] \
  [--signature-prefix PREFIX] \
  [--event-type-path JSON_PATH] \
  [--event-id-path JSON_PATH] \
  [--transform-script FILENAME] \
  [--allow-unsigned]

dispatcher-cli webhook remove <source>
dispatcher-cli webhook list
dispatcher-cli webhook show <source>
dispatcher-cli webhook test <source> [--payload '{"type": "test"}']
dispatcher-cli webhook disable <source>
dispatcher-cli webhook enable <source>
dispatcher-cli webhook stats <source> [--window DURATION] [--alert-threshold N]
```

### Key Options

| Option               | Description                                                                                           |
| -------------------- | ----------------------------------------------------------------------------------------------------- |
| `--secret-env`       | Environment variable holding the webhook secret. Required unless `--allow-unsigned`.                  |
| `--signature-header` | HTTP header containing the signature (e.g., `x-hub-signature-256`).                                   |
| `--signature-algo`   | Verification algorithm: `hmac-sha256-hex` (default), `hmac-sha256-base64`, `hmac-sha1-hex`, `custom`. |
| `--signature-prefix` | Prefix to strip from signature header value before comparison (e.g., `sha256=`).                      |
| `--event-type-path`  | JSONPath to the event type in the payload (e.g., `$.type`).                                           |
| `--event-id-path`    | JSONPath to the event ID for deduplication (e.g., `$.id`).                                            |
| `--transform-script` | Filename (not path) of the transform script in `transforms_dir`.                                      |
| `--allow-unsigned`   | Allow webhooks without signature verification. Prints a security warning.                             |

### Testing

`webhook test` calls `/reload` first, so new agent files are picked up immediately:

```bash
dispatcher-cli webhook test github --payload '{"action": "opened", "issue": {"number": 42, "title": "Bug report", "body": "Something broke"}, "repository": {"full_name": "user/repo"}}'
```

---

## Agent Commands

```bash
dispatcher-cli agent list
dispatcher-cli agent show <agent_id>
dispatcher-cli agent run <agent_id> [--payload '{"key": "value"}']
```

| Command           | Description                                                                             |
| ----------------- | --------------------------------------------------------------------------------------- |
| `agent list`      | List all agents with their status, trigger type, and last run time.                     |
| `agent show <id>` | Show detailed info for a specific agent including config, run history, and queue state. |
| `agent run <id>`  | Manually trigger an agent. Use `--payload` to inject test data.                         |

Agent IDs are path-based: `schedules/daily-summary`, `webhooks/github-issue`.

---

## Channel Commands

```bash
dispatcher-cli channel list
dispatcher-cli channel show <name>
```

| Command | Description |
|---------|-------------|
| `channel list` | List all configured notification channels. |
| `channel show <name>` | Show channel configuration details. |
