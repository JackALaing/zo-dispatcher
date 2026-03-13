#!/usr/bin/env python3
"""
zo-dispatcher — Agent Dispatcher Service

Dispatches Zo agents on schedules (rrule) and webhook events.
Agent definitions are markdown files with YAML frontmatter.
Output is routed to configurable notification channels (Discord, SMS, email, Telegram).
"""

import asyncio
import aiohttp
from aiohttp import web
from collections import deque
import hashlib
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.agents import parse_agent_file, compute_next_run
from zo_dispatcher.webhooks import verify_signature, apply_transform, event_matches, _get_nested_value
from zo_dispatcher.channels import BUILTIN_CHANNELS, MCP_URL, CHANNEL_RETRY_DELAYS

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.json"

# Empirically discovered Zo API error substrings when all compute sessions are occupied.
# If these change upstream, pool retry will stop triggering (falls through to hard error).
SESSION_POOL_ERROR_MARKERS = ["sessions are busy", "cannot evict"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("zo-dispatcher")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _is_session_pool_error(error_text: str) -> bool:
    lower = error_text.lower()
    return any(marker in lower for marker in SESSION_POOL_ERROR_MARKERS)


def compute_jitter(agent_id: str, max_jitter: int) -> float:
    if max_jitter <= 0:
        return 0.0
    h = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16)
    return h % max_jitter


class Dispatcher:
    def __init__(self, config: dict):
        self.config = config
        self.agents_dir = Path(config["agents_dir"])
        self.db = DispatcherDB(config["db_path"])
        self.api_key = os.environ.get("DISPATCHER_ZO_API_KEY")
        if not self.api_key:
            raise ValueError("DISPATCHER_ZO_API_KEY not set")
        self.running = True
        hours_cfg = config.get("notification_hours", {"start": 9, "end": 21})
        self.notify_hour_start = hours_cfg["start"]
        self.notify_hour_end = hours_cfg["end"]
        self.notify_tz = ZoneInfo(config.get("notification_timezone", "America/New_York"))
        self._agents: list[dict] = []
        self._source_locks: dict[str, asyncio.Lock] = {}
        # Concurrency controls:
        #   _dispatch_semaphore — caps concurrent /zo/ask calls from webhook agents only.
        #     Scheduled agents call dispatch_agent() directly, bypassing this limit,
        #     so bursty webhooks can't starve scheduled work.
        #   _source_locks — per-source serialization for webhook events.
        #   _in_flight — tracks all async dispatch tasks for graceful shutdown.
        #   max_runs / max_runs_window (per-agent frontmatter) — caps how many times an
        #     individual agent runs within a time window. Cost/rate control.
        self._dispatch_semaphore = asyncio.Semaphore(config.get("max_concurrent_dispatches", 5))
        self._in_flight: set[asyncio.Task] = set()
        self._max_runs_warned_at: dict[str, float] = {}  # agent_id -> monotonic time of last warning
        self._webhook_rate: dict[str, deque[float]] = {}  # source -> deque of request timestamps
        self._last_parser_error_fingerprint: str | None = None
        self._last_parser_warning_fingerprint: str | None = None
        self._queue_locks: dict[str, asyncio.Lock] = {}
        self._mcp_session_id: str | None = None
        self._mcp_token: str | None = None
        try:
            self._config_mtime: float = CONFIG_PATH.stat().st_mtime
        except OSError:
            self._config_mtime: float = 0

    def _refresh_agents(self):
        self._agents = self.scan_agents()

    def _active_webhook_agents(self) -> list[dict]:
        return [a for a in self._agents if a["trigger"] in ("webhook", "both") and a.get("active", True)]

    def _get_source_lock(self, source: str) -> asyncio.Lock:
        if source not in self._source_locks:
            self._source_locks[source] = asyncio.Lock()
        return self._source_locks[source]

    async def _dispatch_with_limit(self, agent: dict, context: dict | None = None):
        async with self._dispatch_semaphore:
            await self.dispatch_agent(agent, context)

    def _maybe_reload_config(self):
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime > self._config_mtime:
                self.config = load_config()
                self._config_mtime = mtime
                logger.info("Config reloaded")
        except Exception as e:
            logger.warning(f"Config reload failed: {e}")

    def _is_business_hours(self) -> bool:
        now_local = datetime.now(self.notify_tz)
        return self.notify_hour_start <= now_local.hour < self.notify_hour_end

    def _get_mcp_token(self) -> str:
        token = os.environ.get("ZO_CLIENT_IDENTITY_TOKEN")
        if not token:
            raise RuntimeError("ZO_CLIENT_IDENTITY_TOKEN not set")
        return token

    async def _ensure_mcp_session(self):
        if self._mcp_session_id:
            return
        self._mcp_token = self._get_mcp_token()
        headers = {"Authorization": f"Bearer {self._mcp_token}", "Content-Type": "application/json"}
        async with self.http_session.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "zo-dispatcher", "version": "1.0.0"}},
        }) as resp:
            self._mcp_session_id = resp.headers.get("mcp-session-id")
            await resp.json()
        headers["Mcp-Session-Id"] = self._mcp_session_id
        async with self.http_session.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }) as resp:
            pass

    async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> dict:
        await self._ensure_mcp_session()
        headers = {"Authorization": f"Bearer {self._mcp_token}", "Content-Type": "application/json",
                   "Mcp-Session-Id": self._mcp_session_id}
        async with self.http_session.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }) as resp:
            data = await resp.json()
            if "error" in data:
                if "auth" in str(data["error"]).lower():
                    self._mcp_session_id = None
                    self._mcp_token = None
                raise RuntimeError(f"MCP tool error: {data['error']}")
            return data.get("result", {})

    def _queue_notification(self, channel_spec: str, title: str, content: str, conv_id: str = ""):
        self.db.queue_notification(channel_spec, title, content, conv_id)
        logger.info(f"Queued notification '{title}' for business hours")

    async def _drain_notification_queue(self):
        if not self._is_business_hours():
            return
        pending = self.db.pop_pending_notifications()
        if not pending:
            return
        logger.info(f"Business hours — draining {len(pending)} queued notification(s)")
        failed = []
        for notif in pending:
            try:
                await self._deliver(
                    channel_spec=notif["channel_spec"],
                    title=notif["title"],
                    content=notif["content"],
                    conv_id=notif.get("conv_id", ""),
                )
            except Exception:
                failed.append(notif)
        for notif in failed:
            self.db.queue_notification(notif["channel_spec"], notif["title"], notif["content"], notif.get("conv_id", ""))

    def scan_agents(self) -> list[dict]:
        agents = []
        errors: list[tuple[str, str]] = []
        warnings: list[tuple[str, str]] = []
        seen_ids: dict[str, str] = {}

        if not self.agents_dir.exists():
            logger.warning(f"Agents directory {self.agents_dir} does not exist")
            return agents

        for f in sorted(self.agents_dir.rglob("*.md")):
            agent, error = parse_agent_file(f, self.agents_dir)
            if error:
                rel = str(f.relative_to(self.agents_dir))
                errors.append((rel, error))
                continue

            if agent["id"] in seen_ids:
                logger.error(f"Duplicate agent ID '{agent['id']}': {seen_ids[agent['id']]} and {f}")
                agents = [a for a in agents if a["id"] != agent["id"]]
                continue

            for w in agent.pop("_warnings", []):
                warnings.append((agent["id"], w))

            seen_ids[agent["id"]] = str(f)
            agents.append(agent)

        self._handle_parser_errors(errors)
        self._handle_parser_warnings(warnings)
        return agents

    def _handle_parser_errors(self, errors: list[tuple[str, str]]):
        if not errors:
            self._last_parser_error_fingerprint = None
            return

        fingerprint = hashlib.md5(
            json.dumps(sorted(errors)).encode()
        ).hexdigest()

        if fingerprint == self._last_parser_error_fingerprint:
            return

        self._last_parser_error_fingerprint = fingerprint

        lines = [f"**{len(errors)} agent file(s) failed to parse:**\n"]
        for path, error in sorted(errors):
            lines.append(f"- `{path}`: {error}")

        system_channel = self.config.get("system_notification_channel")
        if system_channel:
            asyncio.create_task(self._notify(
                system_channel,
                "Agent parser errors",
                "\n".join(lines),
            ))

    def _handle_parser_warnings(self, warnings: list[tuple[str, str]]):
        if not warnings:
            self._last_parser_warning_fingerprint = None
            return

        fingerprint = hashlib.md5(
            json.dumps(sorted(warnings)).encode()
        ).hexdigest()

        if fingerprint == self._last_parser_warning_fingerprint:
            return

        self._last_parser_warning_fingerprint = fingerprint

        lines = [f"**{len(warnings)} agent warning(s):**\n"]
        for agent_id, warning in sorted(warnings):
            lines.append(f"- `{agent_id}`: {warning}")

        system_channel = self.config.get("system_notification_channel")
        if system_channel:
            asyncio.create_task(self._notify(
                system_channel,
                "Agent parser warnings",
                "\n".join(lines),
            ))

    def _get_queue_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._queue_locks:
            self._queue_locks[agent_id] = asyncio.Lock()
        return self._queue_locks[agent_id]

    def _queues_dir(self) -> Path:
        return self.agents_dir / ".queues"

    def _queue_path(self, agent_id: str) -> Path:
        return self._queues_dir() / f"{agent_id.replace('/', '__')}.jsonl"

    def _snapshot_path(self, agent_id: str, timestamp: str) -> Path:
        return self._queues_dir() / f"{agent_id.replace('/', '__')}.{timestamp}.jsonl"

    def _snapshot_glob(self, agent_id: str) -> str:
        return f"{agent_id.replace('/', '__')}.*.jsonl"

    def _count_deferred_events(self, agent_id: str) -> int:
        count = 0
        qpath = self._queue_path(agent_id)
        if qpath.exists():
            count += sum(1 for line in qpath.read_text().splitlines() if line.strip())
        for snap in self._queues_dir().glob(self._snapshot_glob(agent_id)):
            count += sum(1 for line in snap.read_text().splitlines() if line.strip())
        return count

    async def _append_deferred_event(self, agent_id: str, event_type: str, payload: dict):
        entry = json.dumps({
            "event_type": event_type,
            "payload": payload,
            "received_at": datetime.now(timezone.utc).isoformat(),
        })
        qdir = self._queues_dir()
        qdir.mkdir(parents=True, exist_ok=True)
        qpath = self._queue_path(agent_id)

        async with self._get_queue_lock(agent_id):
            with open(qpath, "a") as f:
                f.write(entry + "\n")

        count = sum(1 for line in qpath.read_text().splitlines() if line.strip())
        logger.info(f"Deferred event for '{agent_id}' (queue: {count} events)")

    def _snapshot_queue(self, agent_id: str) -> Path | None:
        qdir = self._queues_dir()
        qpath = self._queue_path(agent_id)
        glob_pattern = self._snapshot_glob(agent_id)

        leftover_snapshots = sorted(qdir.glob(glob_pattern)) if qdir.exists() else []

        has_queue = qpath.exists() and qpath.stat().st_size > 0
        has_leftovers = bool(leftover_snapshots)

        if not has_queue and not has_leftovers:
            return None

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = self._snapshot_path(agent_id, ts)

        if has_leftovers:
            merged_lines = []
            for old_snap in leftover_snapshots:
                merged_lines.extend(old_snap.read_text().splitlines())
                old_snap.unlink()
            if has_queue:
                merged_lines.extend(qpath.read_text().splitlines())
                qpath.unlink()
            with open(snapshot, "w") as f:
                f.write("\n".join(line for line in merged_lines if line.strip()) + "\n")
        elif has_queue:
            qpath.rename(snapshot)

        return snapshot

    def _cleanup_snapshot(self, snapshot: Path, success: bool):
        if success and snapshot.exists():
            snapshot.unlink()

    def is_due(self, agent: dict) -> bool:
        if not agent.get("active", True):
            return False
        if agent["trigger"] not in ("schedule", "both"):
            return False

        last_run = self.db.get_last_run(agent["id"])
        now = datetime.now(timezone.utc)

        if last_run is None:
            logger.info(f"Agent '{agent['id']}' first seen, seeding last_run to now")
            self.db.mark_run(agent["id"], status="seed")
            return False

        next_run = compute_next_run(agent["rrule"], last_run)
        if next_run is None:
            logger.warning(f"Agent '{agent['id']}' has unparseable rrule, skipping")
            return False

        return next_run <= now

    async def call_zo_ask(self, prompt: str, model: str | None = None,
                          persona_id: str | None = None,
                          timeout_seconds: int | None = None,
                          retry_delays: list[int] | None = None) -> tuple[str, str]:
        model = model or self.config["default_model"]
        payload = {
            "input": prompt,
            "model_name": model,
            "stream": False,
        }
        if persona_id:
            payload["persona_id"] = persona_id

        timeout = aiohttp.ClientTimeout(
            total=timeout_seconds or self.config.get("zo_ask_timeout_seconds", 1800)
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        pool_delays = self.config.get("session_pool_retry_delays", [15, 30, 60, 120])

        conv_id = ""
        for pool_attempt, pool_delay in enumerate(pool_delays):
            async with self.http_session.post(
                f"{self.config['zo_api_url']}/zo/ask",
                headers=headers,
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    if _is_session_pool_error(error_text) and pool_attempt < len(pool_delays) - 1:
                        logger.warning(
                            f"Session pool full ({resp.status}), retry {pool_attempt + 1}/{len(pool_delays)} in {pool_delay}s"
                        )
                        await asyncio.sleep(pool_delay)
                        continue
                    raise Exception(f"Zo API error {resp.status}: {error_text}")

                data = await resp.json()
                output = data.get("output", "")
                conv_id = data.get("conversation_id", "")

            if output and output.strip():
                return output, conv_id
            break

        logger.warning(f"Empty response from zo/ask (conv {conv_id}), attempting recovery")

        return await self._retry_continue(conv_id, model, headers, timeout,
                                           retry_delays or self.config.get("empty_response_retry_delays", [15, 30, 60]))

    async def _retry_continue(self, conv_id: str, model: str, headers: dict,
                              timeout: aiohttp.ClientTimeout,
                              retry_delays: list[int]) -> tuple[str, str]:
        for attempt, delay in enumerate(retry_delays, 1):
            logger.warning(f"Empty response (conv {conv_id}), continue attempt {attempt}/{len(retry_delays)} in {delay}s")
            await asyncio.sleep(delay)

            continue_payload = {
                "input": "Your previous response was empty. If you were interrupted, please continue where you left off. If you finished the work, please respond with your results.",
                "model_name": model,
                "stream": False,
                "conversation_id": conv_id,
            }

            try:
                async with self.http_session.post(
                    f"{self.config['zo_api_url']}/zo/ask",
                    headers=headers,
                    json=continue_payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status == 409:
                        logger.info(f"Conv {conv_id} busy on continue attempt {attempt}, waiting")
                        continue
                    if resp.status != 200:
                        error_text = await resp.text()
                        if _is_session_pool_error(error_text):
                            logger.warning(f"Session pool full on continue attempt {attempt}, will retry")
                            continue
                        raise Exception(f"Zo API error {resp.status} on continue: {error_text}")

                    data = await resp.json()
                    output = data.get("output", "")
                    new_conv_id = data.get("conversation_id", conv_id)

                if output and output.strip():
                    logger.info(f"Continue attempt {attempt} succeeded for conv {conv_id}")
                    return output, new_conv_id
            except Exception as e:
                logger.error(f"Continue attempt {attempt} failed for conv {conv_id}: {e}")

        logger.error(f"All retries exhausted for conv {conv_id}")
        return "", conv_id

    async def _post_to_channel(self, channel_config: dict, title: str, content: str,
                               discord_channel: str | None = None, conv_id: str = ""):
        payload = {
            "title": title,
            "content": content,
            "conversation_id": conv_id,
        }
        if discord_channel:
            payload["channel_name"] = discord_channel

        url = channel_config["url"]
        timeout = aiohttp.ClientTimeout(total=60)
        async with self.http_session.post(url, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.info(f"Channel notification sent: {title}")
                return data
            else:
                error = await resp.text()
                raise RuntimeError(f"Channel notification failed: {resp.status} {error}")

    async def _deliver(self, channel_spec: str, title: str, content: str, conv_id: str = ""):
        parts = channel_spec.split("/", 1)
        channel_name = parts[0]
        discord_channel = parts[1] if len(parts) > 1 else None

        channel_config = self.config.get("channels", {}).get(channel_name)

        if not channel_config and channel_name not in BUILTIN_CHANNELS:
            logger.error(f"Unknown channel: {channel_name}")
            return

        last_error = None
        for attempt, delay in enumerate([0] + CHANNEL_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                if channel_config:
                    await self._post_to_channel(channel_config, title, content, discord_channel, conv_id)
                else:
                    tool_name, build_payload = BUILTIN_CHANNELS[channel_name]
                    await self._call_mcp_tool(tool_name, build_payload(title, content))
                return
            except Exception as e:
                last_error = e
                logger.warning(f"Channel delivery attempt {attempt + 1} failed: {e}")
        logger.error(f"Channel delivery exhausted after {len(CHANNEL_RETRY_DELAYS) + 1} attempts: {last_error}")

    def _prepare_prompt(self, agent: dict, context: dict | None = None,
                        queue_file: Path | str | None = None) -> str:
        prompt = agent["prompt"]
        now = datetime.now(timezone.utc)

        replacements = {
            "{{ date }}": now.strftime("%Y-%m-%d"),
            "{{ timestamp }}": now.isoformat(),
            "{{ agent_id }}": agent["id"],
        }

        if context:
            replacements["{{ payload }}"] = json.dumps(context.get("payload", {}), indent=2)
            replacements["{{ event_type }}"] = context.get("event_type", "")

        if queue_file is not None:
            replacements["{{ queue_file }}"] = str(queue_file)

        for key, value in replacements.items():
            prompt = prompt.replace(key, value)

        return prompt

    async def _notify(self, channel_spec: str, title: str, content: str,
                       conv_id: str = ""):
        if not self._is_business_hours():
            self._queue_notification(channel_spec, title, content, conv_id)
            return
        await self._deliver(channel_spec, title, content, conv_id)

    async def dispatch_agent(self, agent: dict, context: dict | None = None,
                             queue_file: Path | None = None):
        agent_id = agent["id"]
        title = agent["title"]
        notify = agent.get("notify", "errors")
        notify_channel = agent.get("notify_channel")

        prompt = self._prepare_prompt(agent, context, queue_file=queue_file)

        start_time = time.monotonic()
        logger.info(f"Dispatching agent '{agent_id}' ({title})")

        try:
            output, conv_id = await self.call_zo_ask(
                prompt,
                model=agent.get("model"),
                persona_id=agent.get("persona"),
                timeout_seconds=agent.get("timeout"),
                retry_delays=agent.get("retry_delays"),
            )
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.error(f"Agent '{agent_id}' failed: {e}")
            self.db.mark_run(agent_id, status="failure", duration=duration)

            if notify != "never" and notify_channel:
                await self._notify(
                    channel_spec=notify_channel,
                    title=f"[FAILED] {title}",
                    content=f"Agent `{agent_id}` failed:\n\n```\n{e}\n```",
                )
            return

        duration = time.monotonic() - start_time

        if not output or not output.strip():
            logger.error(f"Agent '{agent_id}' empty response (conv {conv_id})")
            self.db.mark_run(agent_id, status="failure", conv_id=conv_id, duration=duration)

            if notify != "never" and notify_channel:
                await self._notify(
                    channel_spec=notify_channel,
                    title=f"[EMPTY] {title}",
                    content=f"Agent `{agent_id}` produced empty output.\n\n"
                            f"**Conversation**: `{conv_id}`\n\n"
                            f"Open the conversation and send \"Please continue\".",
                    conv_id=conv_id,
                )
            return

        # Success
        logger.info(f"Agent '{agent_id}' completed (conv {conv_id}, {len(output)} chars, {duration:.1f}s)")
        self.db.mark_run(agent_id, status="success", conv_id=conv_id, duration=duration)

        if notify == "always" and notify_channel:
            await self._notify(notify_channel, title, output, conv_id)

    # --- Webhook handling ---

    async def handle_webhook(self, request):
        source = request.match_info["source"]
        body = await request.read()

        # Rate limiting — check before any crypto work
        rate_limit = self.config.get("webhook_rate_limit", 60)
        now_mono = time.monotonic()
        if source not in self._webhook_rate:
            self._webhook_rate[source] = deque()
        timestamps = self._webhook_rate[source]
        # Evict entries older than 60 seconds
        while timestamps and timestamps[0] < now_mono - 60:
            timestamps.popleft()
        if len(timestamps) >= rate_limit:
            logger.warning(f"Rate limit exceeded for source: {source} ({len(timestamps)}/{rate_limit} per 60s)")
            return web.json_response({"error": "rate limit exceeded"}, status=429)
        timestamps.append(now_mono)

        source_config = self.db.get_webhook_source(source)
        if not source_config:
            logger.warning(f"Webhook from unknown source: {source}")
            return web.json_response({"received": True})

        if source_config.get("disabled"):
            logger.info(f"Webhook from disabled source: {source}")
            return web.json_response({"error": "source disabled"}, status=403)

        sig_header = source_config.get("signature_header")
        header_value = request.headers.get(sig_header) if sig_header else None

        transforms_dir = Path(self.config["transforms_dir"])
        if not verify_signature(source_config, header_value, body, transforms_dir):
            logger.error(f"Signature verification failed for source: {source} (remote: {request.remote})")
            # Return 200 even on sig failure — returning 4xx would let attackers
            # distinguish valid source names from invalid ones.
            return web.json_response({"received": True})

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from source: {source}")
            return web.json_response({"error": "Invalid JSON"}, status=400)

        event_id = _get_nested_value(payload, source_config.get("event_id_path"))
        if event_id and self.db.check_dedupe(event_id, source):
            logger.info(f"Duplicate event {event_id} from {source}, skipping")
            return web.json_response({"received": True, "duplicate": True})
        if event_id:
            self.db.record_event(event_id, source)

        event_type = _get_nested_value(payload, source_config.get("event_type_path"))

        payload = apply_transform(source_config, payload, event_type or "", transforms_dir)

        if payload is None:
            logger.info(f"Event dropped by transform for source: {source}")
            return web.json_response({"received": True, "dropped": True})

        webhook_agents = self._active_webhook_agents()
        matched = [a for a in webhook_agents
                   if event_matches(a["event"], source, event_type)]

        if not matched:
            logger.warning(f"No agents matched for {source} event: {event_type}")
            return web.json_response({"received": True, "matched": 0})

        logger.info(f"Matched {len(matched)} agent(s) for {source} event: {event_type}")

        context = {
            "payload": payload,
            "event_type": event_type or "",
        }

        task = asyncio.create_task(self._dispatch_webhook_agents(source, matched, context))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

        return web.json_response({"received": True, "matched": len(matched)})

    async def _dispatch_webhook_agents(self, source: str, agents: list[dict], context: dict):
        async with self._get_source_lock(source):
            for agent in agents:
                if agent.get("defer_to_cron"):
                    await self._append_deferred_event(
                        agent["id"],
                        context.get("event_type", ""),
                        context.get("payload", {}),
                    )
                    continue

                max_runs = agent.get("max_runs")
                if max_runs is not None:
                    window = agent.get("max_runs_window", 3600)
                    count = self.db.count_runs_in_window(agent["id"], window)
                    if count >= max_runs:
                        logger.warning(f"Agent '{agent['id']}' hit max_runs ({max_runs}/{window}s), dropping")
                        last_warned = self._max_runs_warned_at.get(agent["id"], 0)
                        if time.monotonic() - last_warned > window:
                            self._max_runs_warned_at[agent["id"]] = time.monotonic()
                            system_channel = self.config.get("system_notification_channel")
                            if system_channel:
                                await self._notify(
                                    system_channel,
                                    f"Dispatch budget exceeded",
                                    f"Agent `{agent['id']}` hit max_runs ({max_runs} per {window}s). Events being dropped.",
                                )
                        continue

                await self._dispatch_with_limit(agent, context)

    # --- HTTP server endpoints ---

    async def start_webhook_server(self):
        app = web.Application()
        app.router.add_post("/webhook/{source}", self.handle_webhook)
        app.router.add_post("/reload", self.handle_reload)
        app.router.add_post("/dispatch", self.handle_dispatch)
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/webhooks", self.handle_list_webhooks)
        app.router.add_get("/channels", self.handle_list_channels)
        app.router.add_get("/agents", self.handle_list_agents)
        app.router.add_get("/agents/{agent_id:.+}", self.handle_show_agent)

        runner = web.AppRunner(app)
        await runner.setup()
        port = self.config.get("webhook_port", 8790)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Webhook server listening on port {port}")
        return runner

    def _audit_webhook_secrets(self) -> list[str]:
        """Check all registered webhook sources for missing secret env vars."""
        warnings = []
        for src in self.db.list_webhook_sources():
            secret_env = src.get("secret_env")
            if secret_env and not os.environ.get(secret_env):
                warnings.append(
                    f"Source '{src['source']}': secret env var '{secret_env}' "
                    f"not set in current process environment"
                )
        for w in warnings:
            logger.warning(f"Webhook secret audit: {w}")
        return warnings

    async def handle_reload(self, request):
        self._refresh_agents()
        self._maybe_reload_config()
        secret_warnings = self._audit_webhook_secrets()
        return web.json_response({
            "reloaded": True,
            "agents_loaded": len(self._agents),
            "secret_warnings": secret_warnings,
        })

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "agents": len(self._agents),
            "in_flight": len(self._in_flight),
        })

    async def handle_list_webhooks(self, request):
        sources = self.db.list_webhook_sources()
        return web.json_response({"sources": sources})

    async def handle_list_channels(self, request):
        channels = self.config.get("channels", {})
        return web.json_response({"custom": channels, "builtin": list(BUILTIN_CHANNELS.keys())})

    async def handle_list_agents(self, request):
        agents = []
        for a in self._agents:
            agents.append({
                "id": a["id"],
                "title": a["title"],
                "trigger": a["trigger"],
                "active": a.get("active", True),
                "event": a.get("event"),
                "rrule": a.get("rrule"),
                "notify_channel": a.get("notify_channel"),
                "notify": a.get("notify"),
            })
        return web.json_response({"agents": agents})

    async def handle_show_agent(self, request):
        agent_id = request.match_info["agent_id"]
        for a in self._agents:
            if a["id"] == agent_id:
                safe = {k: v for k, v in a.items() if k != "prompt"}
                safe["prompt_length"] = len(a.get("prompt", ""))
                last_run = self.db.get_last_run(agent_id)
                safe["last_run"] = last_run.isoformat() if last_run else None
                if a.get("max_runs"):
                    window = a.get("max_runs_window", 3600)
                    safe["runs_in_window"] = self.db.count_runs_in_window(agent_id, window)
                if a.get("defer_to_cron"):
                    safe["deferred_events"] = self._count_deferred_events(agent_id)
                return web.json_response({"agent": safe})
        return web.json_response({"error": "Agent not found"}, status=404)

    async def handle_dispatch(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        agent_id = data.get("agent_id")
        if not agent_id:
            return web.json_response({"error": "agent_id required"}, status=400)

        self._refresh_agents()

        agent = None
        for a in self._agents:
            if a["id"] == agent_id:
                agent = a
                break

        if not agent:
            return web.json_response({"error": f"Agent '{agent_id}' not found"}, status=404)

        context = None
        payload_data = data.get("payload")
        if payload_data:
            context = {
                "payload": payload_data,
                "event_type": data.get("event_type", "manual"),
            }

        task = asyncio.create_task(self._dispatch_with_limit(agent, context))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

        return web.json_response({"dispatched": True, "agent_id": agent_id})

    # --- Graceful shutdown ---

    async def shutdown(self):
        self.running = False
        if self._in_flight:
            timeout = self.config.get("zo_ask_timeout_seconds", 1800)
            logger.info(f"Shutting down, waiting for {len(self._in_flight)} in-flight dispatches")
            done, pending = await asyncio.wait(self._in_flight, timeout=timeout)
            if pending:
                logger.warning(f"Shutdown timeout: {len(pending)} dispatches still running")
        logger.info("Dispatcher shutdown complete")

    # --- Main loops ---

    async def tick(self):
        self._maybe_reload_config()
        self._refresh_agents()
        await self._drain_notification_queue()

        schedule_agents = [a for a in self._agents if a["trigger"] in ("schedule", "both")]
        due = [a for a in schedule_agents if self.is_due(a)]

        if due:
            logger.info(f"Tick: {len(due)} agent(s) due out of {len(schedule_agents)} scheduled")

        # Scheduled agents bypass _dispatch_semaphore — they dispatch directly.
        # Only webhook agents go through _dispatch_with_limit. This prevents
        # bursty webhooks from blocking scheduled agents.
        for agent in due:
            max_runs = agent.get("max_runs")
            if max_runs is not None:
                window = agent.get("max_runs_window", 3600)
                count = self.db.count_runs_in_window(agent["id"], window)
                if count >= max_runs:
                    logger.warning(f"Agent '{agent['id']}' hit max_runs ({max_runs}/{window}s) on scheduled tick, skipping")
                    continue

            snapshot = None
            queue_file = None
            defer_mode = agent.get("defer_to_cron")
            if defer_mode:
                snapshot = self._snapshot_queue(agent["id"])
                if not snapshot and defer_mode == "skip_if_empty":
                    logger.info(f"Agent '{agent['id']}' deferred queue empty, skipping")
                    continue
                queue_file = snapshot if snapshot else "No events queued."

            jitter = compute_jitter(agent["id"], self.config.get("jitter_max_seconds", 0))
            if jitter > 0:
                logger.debug(f"Jitter: sleeping {jitter:.0f}s before dispatching '{agent['id']}'")
                await asyncio.sleep(jitter)

            try:
                await self.dispatch_agent(agent, queue_file=queue_file)
                if snapshot:
                    self._cleanup_snapshot(snapshot, success=True)
            except Exception as e:
                logger.error(f"Unexpected error dispatching '{agent['id']}': {e}", exc_info=True)
                self.db.mark_run(agent["id"], status="failure")
                if snapshot:
                    self._cleanup_snapshot(snapshot, success=False)

        self.db.prune_old_runs(days=7)
        self.db.prune_old_events(hours=self.config.get("dedupe_hours", 24))
        # Clear stale budget warnings so the dict doesn't grow unboundedly
        self._max_runs_warned_at = {k: v for k, v in self._max_runs_warned_at.items()
                               if k in {a["id"] for a in self._agents}}

    async def run(self):
        self.http_session = aiohttp.ClientSession()
        self._refresh_agents()

        runner = await self.start_webhook_server()

        poll_interval = self.config.get("poll_interval_seconds", 60)
        schedule_agents = [a for a in self._agents if a["trigger"] in ("schedule", "both")]
        webhook_agents = self._active_webhook_agents()
        both_agents = [a for a in self._agents if a["trigger"] == "both"]
        parse_errors = self._last_parser_error_fingerprint is not None
        started_msg = (
            f"zo-dispatcher started (poll every {poll_interval}s, "
            f"{len(schedule_agents)} schedule, {len(webhook_agents)} webhook"
        )
        if both_agents:
            started_msg += f", {len(both_agents)} dual-trigger"
        started_msg += f", {len(self._agents)} total"
        if parse_errors:
            started_msg += ", parse errors present"
        started_msg += ")"
        logger.info(started_msg)

        self._audit_webhook_secrets()

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.shutdown()))

        try:
            while self.running:
                try:
                    await self.tick()
                except Exception as e:
                    logger.error(f"Dispatcher tick error: {e}", exc_info=True)
                await asyncio.sleep(poll_interval)
        finally:
            await runner.cleanup()
            await self.http_session.close()


async def main():
    cfg = load_config()
    dispatcher = Dispatcher(cfg)
    await dispatcher.run()


if __name__ == "__main__":
    asyncio.run(main())
