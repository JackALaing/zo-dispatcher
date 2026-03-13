#!/usr/bin/env python3
"""
zo-dispatcher CLI — manage webhook sources, agents, and channels.

Usage:
    dispatcher-cli webhook add <source> [options]
    dispatcher-cli webhook update <source> [options]
    dispatcher-cli webhook remove <source>
    dispatcher-cli webhook list
    dispatcher-cli webhook show <source>
    dispatcher-cli webhook test <source> [--payload JSON]
    dispatcher-cli webhook disable <source>
    dispatcher-cli webhook enable <source>
    dispatcher-cli webhook stats <source> [--window DURATION] [--alert-threshold N]
    dispatcher-cli webhook providers
    dispatcher-cli webhook rotate <source>
    dispatcher-cli webhook rotate-cleanup <source>

    dispatcher-cli channel list
    dispatcher-cli channel show <name>

    dispatcher-cli agent list
    dispatcher-cli agent show <agent_id>
    dispatcher-cli agent run <agent_id> [--payload JSON]
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.agents import parse_agent_file, compute_next_run

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.json"
PROVIDERS_PATH = Path(__file__).resolve().parent.parent / "config" / "providers.yaml"
DISPATCHER_URL = "http://localhost:8790"
LOKI_URL = "http://localhost:3100"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_providers() -> dict:
    """Load provider blueprints from providers.yaml. Returns {name: config} dict."""
    if not PROVIDERS_PATH.exists():
        return {}
    with open(PROVIDERS_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("providers", {}) if data else {}


def get_db(config: dict):
    return DispatcherDB(config["db_path"]).conn


def get_db_instance(config: dict) -> DispatcherDB:
    return DispatcherDB(config["db_path"])


def _parse_duration(s: str) -> int:
    m = re.match(r"^(\d+)(s|m|h|d)$", s)
    if not m:
        print(f"Error: Invalid duration '{s}'. Use format like 5m, 1h, 30s, 1d.", file=sys.stderr)
        sys.exit(1)
    value, unit = int(m.group(1)), m.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def _format_relative(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    diff = dt - now
    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        total_seconds = abs(total_seconds)
        suffix = "ago"
    else:
        suffix = ""

    if total_seconds < 60:
        result = f"{total_seconds}s"
    elif total_seconds < 3600:
        result = f"{total_seconds // 60}m"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        result = f"{hours}h{minutes}m" if minutes else f"{hours}h"
    else:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        result = f"{days}d{hours}h" if hours else f"{days}d"

    return f"{result} {suffix}".strip() if suffix else f"in {result}"


def _query_loki(query: str, start_ns: int, end_ns: int, limit: int = 5000) -> list[str]:
    params = urllib.parse.urlencode({
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
    })
    url = f"{LOKI_URL}/loki/api/v1/query_range?{params}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            lines = []
            for stream in data.get("data", {}).get("result", []):
                for ts, line in stream.get("values", []):
                    lines.append(line)
            return lines
    except Exception:
        return []


# --- Webhook commands ---

def cmd_webhook_add(args, config):
    db = get_db(config)
    now = datetime.now(timezone.utc).isoformat()

    existing = db.execute("SELECT source FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if existing:
        print(f"Error: Source '{args.source}' already exists. Remove it first.", file=sys.stderr)
        sys.exit(1)

    # Load blueprint if available — CLI flags override blueprint values
    providers = load_providers()
    blueprint = providers.get(args.source, {})
    if blueprint:
        print(f"Using blueprint for '{args.source}'")

    def resolve(cli_val, blueprint_key, default=None):
        """CLI flag wins over blueprint, blueprint wins over default."""
        if cli_val is not None:
            return cli_val
        bp_val = blueprint.get(blueprint_key)
        # Treat YAML null as None
        if bp_val is not None:
            return str(bp_val) if not isinstance(bp_val, str) else bp_val
        return default

    secret_env = resolve(args.secret_env, "secret_env")
    signature_header = resolve(args.signature_header, "signature_header")
    signature_prefix = resolve(args.signature_prefix, "signature_prefix", "")
    event_type_path = resolve(args.event_type_path, "event_type_path")
    event_id_path = resolve(args.event_id_path, "event_id_path")
    transform_script = resolve(args.transform_script, "transform_script")

    algo = resolve(args.signature_algo, "signature_algo")
    if secret_env and not algo:
        algo = "hmac-sha256-hex"

    allow_unsigned = getattr(args, "allow_unsigned", False)
    if not secret_env and algo != "custom":
        if not allow_unsigned:
            print(f"Error: No --secret-env set for '{args.source}'. "
                  f"Pass --allow-unsigned to accept unsigned webhooks (not recommended).", file=sys.stderr)
            sys.exit(1)
        print(f"WARNING: Source '{args.source}' will accept unsigned webhooks. "
              f"Any request to /webhook/{args.source} will be processed without verification.", file=sys.stderr)

    db.execute(
        "INSERT INTO webhooks (source, secret_env, signature_header, signature_algo, "
        "signature_prefix, event_type_path, event_id_path, transform_script, allow_unsigned, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.source, secret_env, signature_header, algo,
         signature_prefix, event_type_path, event_id_path,
         transform_script, 1 if allow_unsigned else 0, now, now)
    )
    db.commit()
    print(f"Added webhook source: {args.source}")
    if algo:
        print(f"  Signature: {algo}")
    if secret_env:
        print(f"  Secret env: {secret_env}")
    if signature_header:
        print(f"  Header: {signature_header}")
    if event_type_path:
        print(f"  Event type path: {event_type_path}")


def cmd_webhook_update(args, config):
    db = get_db(config)
    row = db.execute("SELECT * FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if not row:
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)

    cols = [d[0] for d in db.execute("SELECT * FROM webhooks LIMIT 0").description]
    current = dict(zip(cols, row))

    updates = {}
    flag_map = {
        "secret_env": "secret_env",
        "signature_header": "signature_header",
        "signature_algo": "signature_algo",
        "signature_prefix": "signature_prefix",
        "event_type_path": "event_type_path",
        "event_id_path": "event_id_path",
        "transform_script": "transform_script",
    }
    for attr, col in flag_map.items():
        val = getattr(args, attr, None)
        if val is not None:
            updates[col] = val

    if getattr(args, "allow_unsigned", False):
        updates["allow_unsigned"] = 1
    elif getattr(args, "disallow_unsigned", False):
        updates["allow_unsigned"] = 0

    if not updates:
        print("Nothing to update. Pass at least one --flag.", file=sys.stderr)
        sys.exit(1)

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [args.source]
    db.execute(f"UPDATE webhooks SET {set_clause} WHERE source = ?", values)
    db.commit()

    print(f"Updated webhook source: {args.source}")
    for k, v in updates.items():
        if k != "updated_at":
            print(f"  {k}: {current.get(k, '')} -> {v}")


def cmd_webhook_remove(args, config):
    db = get_db(config)
    existing = db.execute("SELECT source FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if not existing:
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)
    db.execute("DELETE FROM webhook_events WHERE source = ?", (args.source,))
    db.execute("DELETE FROM webhooks WHERE source = ?", (args.source,))
    db.commit()
    print(f"Removed webhook source: {args.source}")


def cmd_webhook_list(args, config):
    db = get_db(config)
    rows = db.execute("SELECT * FROM webhooks ORDER BY source").fetchall()
    if not rows:
        print("No webhook sources registered.")
        return
    cols = [d[0] for d in db.execute("SELECT * FROM webhooks LIMIT 0").description]
    for row in rows:
        data = dict(zip(cols, row))
        parts = [data["source"]]
        if data.get("signature_algo"):
            parts.append(f"algo={data['signature_algo']}")
        if data.get("event_type_path"):
            parts.append(f"event_path={data['event_type_path']}")
        if data.get("allow_unsigned"):
            parts.append("[UNSIGNED]")
        if data.get("disabled"):
            parts.append("[DISABLED]")
        print("  ".join(parts))


def cmd_webhook_show(args, config):
    db = get_db(config)
    row = db.execute("SELECT * FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if not row:
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)
    cols = [d[0] for d in db.execute("SELECT * FROM webhooks LIMIT 0").description]
    data = dict(zip(cols, row))
    print(json.dumps(data, indent=2))


def cmd_webhook_test(args, config):
    payload = json.loads(args.payload) if args.payload else {"type": "test.event", "id": "test_" + datetime.now().strftime("%H%M%S")}

    try:
        resp = requests.post(f"{DISPATCHER_URL}/webhook/{args.source}",
                             json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        print(f"Status: {resp.status_code}")
        print(json.dumps(resp.json(), indent=2))
    except requests.ConnectionError:
        print("Error: Cannot connect to dispatcher. Is it running?", file=sys.stderr)
        sys.exit(1)


def cmd_webhook_providers(args, config):
    providers = load_providers()
    if not providers:
        print("No providers.yaml found.")
        return
    print(f"{len(providers)} providers available:\n")
    for name, p in sorted(providers.items()):
        algo = p.get("signature_algo", "?")
        events_count = len(p.get("events", []))
        print(f"  {name:<20} {p.get('display_name', name):<20} algo={algo}  ({events_count} events)")


def cmd_webhook_disable(args, config):
    db_instance = get_db_instance(config)
    if not db_instance.set_webhook_disabled(args.source, True):
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)
    print(f"Disabled webhook source: {args.source}")
    print("  All incoming requests will be rejected with 403 immediately.")


def cmd_webhook_enable(args, config):
    db_instance = get_db_instance(config)
    if not db_instance.set_webhook_disabled(args.source, False):
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)
    print(f"Enabled webhook source: {args.source}")


def cmd_webhook_stats(args, config):
    source = args.source
    window_str = args.window or "1h"
    window_seconds = _parse_duration(window_str)

    db = get_db(config)
    row = db.execute("SELECT source FROM webhooks WHERE source = ?", (source,)).fetchone()
    if not row:
        print(f"Error: Source '{source}' not found.", file=sys.stderr)
        sys.exit(1)

    # DB stats (always available)
    db_instance = get_db_instance(config)
    run_counts = db_instance.count_runs_for_source(source, window_seconds)
    dispatched = sum(run_counts.get(s, 0) for s in ("success", "failure"))
    deduped = db_instance.count_deduped_for_source(source, window_seconds)

    # Loki stats
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    start_ns = int((datetime.now(timezone.utc).timestamp() - window_seconds) * 1e9)

    loki_available = True
    log_file = '/dev/shm/zo-dispatcher.log'

    # Total requests for this source (webhook handler log lines)
    request_lines = _query_loki(
        f'{{filename="{log_file}"}} |~ "source.*{source}|{source}.*event"',
        start_ns, now_ns
    )

    # Signature failures
    sig_fail_lines = _query_loki(
        f'{{filename="{log_file}"}} |~ "Signature verification failed" |~ "{source}"',
        start_ns, now_ns
    )

    # Unknown source rejections
    unknown_lines = _query_loki(
        f'{{filename="{log_file}"}} |~ "unknown source.*{source}"',
        start_ns, now_ns
    )

    # max_runs budget drops
    dropped_lines = _query_loki(
        f'{{filename="{log_file}"}} |~ "hit max_runs" |~ "{source}"',
        start_ns, now_ns
    )

    # Disabled source rejections
    disabled_lines = _query_loki(
        f'{{filename="{log_file}"}} |~ "disabled source.*{source}"',
        start_ns, now_ns
    )

    if not request_lines and not sig_fail_lines:
        # Loki might not be available
        try:
            urllib.request.urlopen(f"{LOKI_URL}/ready", timeout=3)
        except Exception:
            loki_available = False

    rejected = len(sig_fail_lines)
    total_requests = len(request_lines)
    dropped = len(dropped_lines)
    disabled_count = len(disabled_lines)

    print(f"{source} (last {window_str}):")
    if loki_available:
        accepted = max(0, total_requests - rejected - len(unknown_lines) - disabled_count)
        print(f"  requests:   {total_requests:>5}")
        print(f"  rejected:   {rejected:>5}    (signature failures)")
        if disabled_count:
            print(f"  disabled:   {disabled_count:>5}    (source was disabled)")
        print(f"  accepted:   {accepted:>5}")
        print(f"  deduped:    {deduped:>5}    (event ID already seen)")
        print(f"  dispatched: {dispatched:>5}    (agent runs)")
        if dropped:
            print(f"  dropped:    {dropped:>5}    (max_runs budget exceeded)")
    else:
        print("  Warning: Loki unavailable. Showing DB-only stats.", file=sys.stderr)
        print(f"  dispatched: {dispatched:>5}    (agent runs)")
        print(f"  deduped:    {deduped:>5}    (event ID already seen)")

    # Alert threshold check
    alert_threshold = getattr(args, "alert_threshold", None)
    if alert_threshold is not None and rejected >= alert_threshold:
        system_channel = config.get("system_notification_channel")
        if system_channel:
            parts = system_channel.split("/", 1)
            channel_name = parts[0]
            sub_channel = parts[1] if len(parts) > 1 else None
            channel_config = config.get("channels", {}).get(channel_name)
            if channel_config:
                alert_payload = {
                    "title": "Webhook security alert",
                    "content": (
                        f"Source `{source}` had {rejected} signature failure(s) "
                        f"in the last {window_str}.\n\n"
                        f"Review: `dispatcher-cli webhook stats {source}`"
                    ),
                }
                if sub_channel:
                    alert_payload["channel_name"] = sub_channel
                try:
                    requests.post(channel_config["url"], json=alert_payload, timeout=10)
                    print(f"\n  Alert sent to {system_channel}: {rejected} signature failures >= threshold {alert_threshold}")
                except Exception as e:
                    print(f"\n  Warning: Failed to send alert to {system_channel}: {e}", file=sys.stderr)
        sys.exit(1)


ZO_SECRETS_PATH = Path("/root/.zo_secrets")


def _read_secret_value(env_name: str) -> str | None:
    import subprocess
    result = subprocess.run(
        ["bash", "-c", f"source {ZO_SECRETS_PATH} && echo -n \"${{{env_name}}}\""],
        capture_output=True, text=True
    )
    val = result.stdout
    return val if val else None


def _write_secret(env_name: str, value: str):
    with open(ZO_SECRETS_PATH, "a") as f:
        f.write(f'\nexport {env_name}="{value}"\n')


def _remove_secret(env_name: str):
    lines = ZO_SECRETS_PATH.read_text().splitlines(keepends=True)
    filtered = [l for l in lines if not l.strip().startswith(f"export {env_name}=")]
    ZO_SECRETS_PATH.write_text("".join(filtered))


def cmd_webhook_rotate(args, config):
    db = get_db(config)
    row = db.execute("SELECT * FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if not row:
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)

    cols = [d[0] for d in db.execute("SELECT * FROM webhooks LIMIT 0").description]
    current = dict(zip(cols, row))
    secret_env = current.get("secret_env", "")

    if not secret_env:
        print(f"Error: Source '{args.source}' has no secret_env configured.", file=sys.stderr)
        sys.exit(1)

    primary = secret_env.split(",")[0].strip()
    if "," in secret_env:
        print(f"Error: Rotation already in progress (secret_env={secret_env}). "
              f"Run 'webhook rotate-cleanup {args.source}' first.", file=sys.stderr)
        sys.exit(1)

    current_value = _read_secret_value(primary)
    if not current_value:
        print(f"Error: Could not read current value of {primary}.", file=sys.stderr)
        sys.exit(1)

    old_name = f"{primary}_OLD"
    _write_secret(old_name, current_value)

    new_secret_env = f"{primary},{old_name}"
    now = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE webhooks SET secret_env = ?, updated_at = ? WHERE source = ?",
               (new_secret_env, now, args.source))
    db.commit()

    print(f"Rotation started for '{args.source}':")
    print(f"  Copied {primary} → {old_name}")
    print(f"  secret_env: {primary} → {new_secret_env}")
    print()
    print("Next steps:")
    print(f"  1. Generate a new secret at your provider")
    print(f"  2. Update {primary} in Settings > Advanced with the new value")
    print(f"  3. Update the secret at your provider")
    print(f"  4. After confirming webhooks work: dispatcher-cli webhook rotate-cleanup {args.source}")


def cmd_webhook_rotate_cleanup(args, config):
    db = get_db(config)
    row = db.execute("SELECT * FROM webhooks WHERE source = ?", (args.source,)).fetchone()
    if not row:
        print(f"Error: Source '{args.source}' not found.", file=sys.stderr)
        sys.exit(1)

    cols = [d[0] for d in db.execute("SELECT * FROM webhooks LIMIT 0").description]
    current = dict(zip(cols, row))
    secret_env = current.get("secret_env", "")

    if "," not in secret_env:
        print(f"No rotation in progress for '{args.source}' (secret_env={secret_env}).")
        return

    parts = [s.strip() for s in secret_env.split(",")]
    primary = parts[0]
    old_names = parts[1:]

    now = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE webhooks SET secret_env = ?, updated_at = ? WHERE source = ?",
               (primary, now, args.source))
    db.commit()

    for old_name in old_names:
        _remove_secret(old_name)

    print(f"Rotation cleanup complete for '{args.source}':")
    print(f"  secret_env: {secret_env} → {primary}")
    for old_name in old_names:
        print(f"  Removed {old_name} from secrets")



# --- Channel commands ---

def cmd_channel_list(args, config):
    channels = config.get("channels", {})
    builtins = ["sms", "email", "telegram"]
    print("Builtin channels:")
    for b in builtins:
        print(f"  {b}")
    print("\nCustom channels:")
    if not channels:
        print("  (none)")
    for name, cfg in channels.items():
        print(f"  {name} -> {cfg.get('url', '?')}")


def cmd_channel_show(args, config):
    name = args.name
    if name in ("sms", "email", "telegram"):
        print(json.dumps({"name": name, "type": "builtin", "delivery": "MCP tool call"}, indent=2))
        return
    channels = config.get("channels", {})
    if name not in channels:
        print(f"Error: Channel '{name}' not found.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"name": name, "type": "custom", **channels[name]}, indent=2))


# --- Agent commands ---

def cmd_agent_list(args, config):
    try:
        requests.post(f"{DISPATCHER_URL}/reload", timeout=5)
    except requests.ConnectionError:
        pass

    try:
        resp = requests.get(f"{DISPATCHER_URL}/agents", timeout=5)
        agents = resp.json().get("agents", [])
    except requests.ConnectionError:
        print("Warning: Dispatcher not running. Scanning agent files directly (no runtime state).", file=sys.stderr)
        agents_dir = Path(config["agents_dir"])
        agents = []
        for f in sorted(agents_dir.rglob("*.md")):
            agent, error = parse_agent_file(f, agents_dir)
            if agent:
                agents.append(agent)

    if not agents:
        print("No agents found.")
        return

    schedule = [a for a in agents if a["trigger"] in ("schedule", "both")]
    webhook = [a for a in agents if a["trigger"] in ("webhook", "both")]

    now = datetime.now(timezone.utc)

    if schedule:
        # Compute next run for each and sort chronologically
        schedule_with_next = []
        for a in schedule:
            rrule = a.get("rrule")
            next_run = None
            if rrule:
                next_run = compute_next_run(rrule, now)
            schedule_with_next.append((a, next_run))

        # Sort: active agents with next_run first (by time), then inactive, then no-next-run
        def sort_key(item):
            a, nr = item
            active = a.get("active", True)
            if not active:
                return (2, datetime.max.replace(tzinfo=timezone.utc))
            if nr is None:
                return (1, datetime.max.replace(tzinfo=timezone.utc))
            return (0, nr)

        schedule_with_next.sort(key=sort_key)

        print(f"Scheduled ({len(schedule)}):")
        for a, next_run in schedule_with_next:
            status = "" if a.get("active", True) else " [inactive]"
            tag = " [both]" if a["trigger"] == "both" else ""
            timing = ""
            if next_run and a.get("active", True):
                timing = f" ({_format_relative(next_run)})"
            print(f"  {a['id']}: {a.get('title', a['id'])}{tag}{timing}{status}")

    if webhook:
        if schedule:
            print()
        print(f"Webhook ({len(webhook)}):")
        for a in webhook:
            status = "" if a.get("active", True) else " [inactive]"
            tag = " [both]" if a["trigger"] == "both" else ""
            event = a.get("event")
            if isinstance(event, list):
                event_str = event[0] if event else "?"
                if len(event) > 1:
                    event_str += f" (+{len(event) - 1} more)"
            else:
                event_str = event or "?"
            print(f"  {a['id']}: {a.get('title', a['id'])} -> {event_str}{tag}{status}")


def cmd_agent_show(args, config):
    try:
        requests.post(f"{DISPATCHER_URL}/reload", timeout=5)
        resp = requests.get(f"{DISPATCHER_URL}/agents/{args.agent_id}", timeout=5)
        if resp.status_code == 404:
            print(f"Error: Agent '{args.agent_id}' not found.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(resp.json().get("agent", {}), indent=2))
    except requests.ConnectionError:
        print("Error: Cannot connect to dispatcher. Is it running?", file=sys.stderr)
        sys.exit(1)


def cmd_agent_run(args, config):
    payload_data = json.loads(args.payload) if args.payload else None
    body = {"agent_id": args.agent_id}
    if payload_data:
        body["payload"] = payload_data

    try:
        resp = requests.post(f"{DISPATCHER_URL}/dispatch", json=body, timeout=10)
        print(f"Status: {resp.status_code}")
        print(json.dumps(resp.json(), indent=2))
    except requests.ConnectionError:
        print("Error: Cannot connect to dispatcher. Is it running?", file=sys.stderr)
        sys.exit(1)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="zo-dispatcher CLI")
    sub = parser.add_subparsers(dest="resource")

    # webhook
    wh = sub.add_parser("webhook")
    wh_sub = wh.add_subparsers(dest="action")

    wh_add = wh_sub.add_parser("add")
    wh_add.add_argument("source")
    wh_add.add_argument("--secret-env", dest="secret_env")
    wh_add.add_argument("--signature-header", dest="signature_header")
    wh_add.add_argument("--signature-algo", dest="signature_algo")
    wh_add.add_argument("--signature-prefix", dest="signature_prefix")
    wh_add.add_argument("--event-type-path", dest="event_type_path")
    wh_add.add_argument("--event-id-path", dest="event_id_path")
    wh_add.add_argument("--transform-script", dest="transform_script")
    wh_add.add_argument("--allow-unsigned", dest="allow_unsigned", action="store_true",
                        help="Accept webhooks without signature verification (not recommended)")

    wh_upd = wh_sub.add_parser("update")
    wh_upd.add_argument("source")
    wh_upd.add_argument("--secret-env", dest="secret_env")
    wh_upd.add_argument("--signature-header", dest="signature_header")
    wh_upd.add_argument("--signature-algo", dest="signature_algo")
    wh_upd.add_argument("--signature-prefix", dest="signature_prefix")
    wh_upd.add_argument("--event-type-path", dest="event_type_path")
    wh_upd.add_argument("--event-id-path", dest="event_id_path")
    wh_upd.add_argument("--transform-script", dest="transform_script")
    wh_upd.add_argument("--allow-unsigned", dest="allow_unsigned", action="store_true",
                        help="Accept webhooks without signature verification")
    wh_upd.add_argument("--no-allow-unsigned", dest="disallow_unsigned", action="store_true",
                        help="Require signature verification (revoke allow-unsigned)")

    wh_rm = wh_sub.add_parser("remove")
    wh_rm.add_argument("source")

    wh_sub.add_parser("list")

    wh_show = wh_sub.add_parser("show")
    wh_show.add_argument("source")

    wh_test = wh_sub.add_parser("test")
    wh_test.add_argument("source")
    wh_test.add_argument("--payload")

    wh_disable = wh_sub.add_parser("disable")
    wh_disable.add_argument("source")

    wh_enable = wh_sub.add_parser("enable")
    wh_enable.add_argument("source")

    wh_stats = wh_sub.add_parser("stats")
    wh_stats.add_argument("source")
    wh_stats.add_argument("--window", default="1h",
                          help="Time window for stats (e.g., 5m, 1h, 24h, 1d). Default: 1h")
    wh_stats.add_argument("--alert-threshold", dest="alert_threshold", type=int, default=None,
                          help="Alert if rejected count >= N. Sends notification and exits with code 1.")

    wh_sub.add_parser("providers", help="List available provider blueprints from providers.yaml")

    wh_rotate = wh_sub.add_parser("rotate", help="Start zero-downtime secret rotation")
    wh_rotate.add_argument("source")

    wh_rotate_cleanup = wh_sub.add_parser("rotate-cleanup", help="Finish secret rotation and remove old secret")
    wh_rotate_cleanup.add_argument("source")

    # channel
    ch = sub.add_parser("channel")
    ch_sub = ch.add_subparsers(dest="action")
    ch_sub.add_parser("list")
    ch_show = ch_sub.add_parser("show")
    ch_show.add_argument("name")

    # agent
    ag = sub.add_parser("agent")
    ag_sub = ag.add_subparsers(dest="action")
    ag_sub.add_parser("list")
    ag_show = ag_sub.add_parser("show")
    ag_show.add_argument("agent_id")
    ag_run = ag_sub.add_parser("run")
    ag_run.add_argument("agent_id")
    ag_run.add_argument("--payload")

    args = parser.parse_args()
    if not args.resource:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    dispatch = {
        ("webhook", "add"): cmd_webhook_add,
        ("webhook", "update"): cmd_webhook_update,
        ("webhook", "remove"): cmd_webhook_remove,
        ("webhook", "list"): cmd_webhook_list,
        ("webhook", "show"): cmd_webhook_show,
        ("webhook", "test"): cmd_webhook_test,
        ("webhook", "disable"): cmd_webhook_disable,
        ("webhook", "enable"): cmd_webhook_enable,
        ("webhook", "stats"): cmd_webhook_stats,
        ("webhook", "providers"): cmd_webhook_providers,
        ("webhook", "rotate"): cmd_webhook_rotate,
        ("webhook", "rotate-cleanup"): cmd_webhook_rotate_cleanup,
        ("channel", "list"): cmd_channel_list,
        ("channel", "show"): cmd_channel_show,
        ("agent", "list"): cmd_agent_list,
        ("agent", "show"): cmd_agent_show,
        ("agent", "run"): cmd_agent_run,
    }

    key = (args.resource, args.action)
    if key not in dispatch:
        parser.print_help()
        sys.exit(1)

    dispatch[key](args, config)


if __name__ == "__main__":
    main()
