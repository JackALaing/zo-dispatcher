"""Tests for notification routing: custom channels, builtins, business hours, retry."""

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zo_dispatcher.server import Dispatcher
from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.channels import CHANNEL_RETRY_DELAYS, BUILTIN_CHANNELS


# --- Helpers ---

CONFIG = {
    "agents_dir": "/tmp/test-agents",
    "db_path": "",
    "zo_api_url": "https://api.zo.computer",
    "default_model": "byok:test",
    "poll_interval_seconds": 60,
    "transforms_dir": "/tmp/transforms",
    "webhook_port": 8790,
    "max_concurrent_dispatches": 5,
    "zo_ask_timeout_seconds": 1800,
    "retry_delays": [15, 30, 60],
    "session_pool_retry_delays": [15, 30, 60, 120],
    "notification_hours": {"start": 0, "end": 24},
    "notification_timezone": "America/New_York",
    "dedupe_hours": 24,
    "system_notification_channel": "discord/general",
    "channels": {
        "discord": {
            "url": "http://localhost:8787/notify",
        }
    },
}


def make_dispatcher(config_override=None):
    tmpdb = tempfile.mktemp(suffix=".db")
    cfg = {**CONFIG, "db_path": tmpdb}
    if config_override:
        cfg.update(config_override)
    os.environ.setdefault("DISPATCHER_ZO_API_KEY", "test_key")
    d = Dispatcher(cfg)
    d.http_session = MagicMock()
    return d, tmpdb


def make_agent(**overrides):
    base = {
        "id": "schedules/test-agent",
        "filepath": "/tmp/test.md",
        "trigger": "schedule",
        "rrule": "RRULE:FREQ=DAILY;BYHOUR=12",
        "event": None,
        "model": "byok:test",
        "persona": None,
        "active": True,
        "title": "Test Agent",
        "notify_channel": "discord/general",
        "notify": "errors",
        "timeout": None,
        "retry_delays": None,
        "max_runs": None,
        "max_runs_window": 3600,
        "prompt": "Do a thing.",
    }
    base.update(overrides)
    return base


class FakeResp:
    status = 200
    async def json(self):
        return {"success": True, "thread_id": "t123"}
    async def text(self):
        return ""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass


class FakeSession:
    def __init__(self):
        self.calls = []
    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return FakeResp()


# --- Custom channel (Discord) tests ---

class TestCustomChannelDelivery:
    def test_post_to_channel(self):
        d, tmpdb = make_dispatcher()

        captured = {}

        class CapturingResp(FakeResp):
            pass

        class CapturingSession:
            def post(self, url, json=None, timeout=None):
                captured["url"] = url
                captured["json"] = json
                return CapturingResp()

        async def run():
            d.http_session = CapturingSession()
            channel_config = d.config["channels"]["discord"]
            result = await d._post_to_channel(
                channel_config, "Test Title", "Test content", "general", "con_123"
            )
            assert captured["url"] == "http://localhost:8787/notify"
            assert captured["json"]["channel_name"] == "general"
            assert captured["json"]["title"] == "Test Title"
            assert captured["json"]["content"] == "Test content"
            assert captured["json"]["conversation_id"] == "con_123"
            assert result["success"] is True

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_deliver_routes_to_post(self):
        d, tmpdb = make_dispatcher()
        session = FakeSession()

        async def run():
            d.http_session = session
            await d._deliver("discord/general", "[FAILED] Test", "Error details")
            assert len(session.calls) == 1
            assert session.calls[0]["url"] == "http://localhost:8787/notify"
            assert session.calls[0]["json"]["title"] == "[FAILED] Test"
            assert session.calls[0]["json"]["channel_name"] == "general"

        asyncio.run(run())
        os.unlink(tmpdb)


# --- Builtin channel (MCP) tests ---

class TestBuiltinChannelDelivery:
    def test_sms_delivery(self):
        d, tmpdb = make_dispatcher()
        mcp_calls = []

        async def fake_mcp(tool_name, arguments):
            mcp_calls.append({"tool": tool_name, "args": arguments})
            return {"success": True}

        async def run():
            with patch.object(d, "_call_mcp_tool", fake_mcp):
                await d._deliver("sms", "Test Agent", "Agent output", "con_x")
                assert len(mcp_calls) == 1
                assert mcp_calls[0]["tool"] == "send_sms_to_user"
                assert "Test Agent" in mcp_calls[0]["args"]["message"]
                assert "Agent output" in mcp_calls[0]["args"]["message"]

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_email_delivery(self):
        d, tmpdb = make_dispatcher()
        mcp_calls = []

        async def fake_mcp(tool_name, arguments):
            mcp_calls.append({"tool": tool_name, "args": arguments})
            return {"success": True}

        async def run():
            with patch.object(d, "_call_mcp_tool", fake_mcp):
                await d._deliver("email", "My Report", "Report content", "con_x")
                assert mcp_calls[0]["tool"] == "send_email_to_user"
                assert mcp_calls[0]["args"]["subject"] == "My Report"
                assert mcp_calls[0]["args"]["markdown_body"] == "Report content"

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_telegram_delivery(self):
        d, tmpdb = make_dispatcher()
        mcp_calls = []

        async def fake_mcp(tool_name, arguments):
            mcp_calls.append({"tool": tool_name, "args": arguments})
            return {"success": True}

        async def run():
            with patch.object(d, "_call_mcp_tool", fake_mcp):
                await d._deliver("telegram", "Test Agent", "Agent output", "con_x")
                assert mcp_calls[0]["tool"] == "send_telegram_message"
                assert "Agent output" in mcp_calls[0]["args"]["message"]

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_custom_posts_builtin_calls_mcp(self):
        d, tmpdb = make_dispatcher()
        post_calls = []
        mcp_calls = []

        class TrackingSession:
            def post(self, url, json=None, timeout=None):
                post_calls.append({"url": url, "json": json})
                return FakeResp()

        async def fake_mcp(tool_name, arguments):
            mcp_calls.append({"tool": tool_name, "args": arguments})
            return {"success": True}

        async def run():
            d.http_session = TrackingSession()
            with patch.object(d, "_call_mcp_tool", fake_mcp):
                await d._deliver("discord/general", "Title", "Content", "con_x")
                assert len(post_calls) == 1
                assert len(mcp_calls) == 0

                post_calls.clear()
                await d._deliver("sms", "Title", "Content", "con_x")
                assert len(mcp_calls) == 1
                assert len(post_calls) == 0

                mcp_calls.clear()
                await d._deliver("email", "Title", "Content", "con_x")
                assert len(mcp_calls) == 1

                mcp_calls.clear()
                await d._deliver("telegram", "Title", "Content", "con_x")
                assert len(mcp_calls) == 1

        asyncio.run(run())
        os.unlink(tmpdb)


# --- Business hours queueing ---

class TestBusinessHours:
    def test_queue_outside_drain_inside(self):
        d, tmpdb = make_dispatcher({"notification_hours": {"start": 25, "end": 26}})
        deliver_calls = []

        async def track_deliver(*args, **kwargs):
            deliver_calls.append(args)

        async def run():
            await d._notify(
                channel_spec="discord/general",
                title="[FAILED] Queued Test",
                content="Should be queued",
            )

            pending = d.db.conn.execute("SELECT * FROM pending_notifications").fetchall()
            assert len(pending) == 1
            assert pending[0]["title"] == "[FAILED] Queued Test"
            assert pending[0]["channel_spec"] == "discord/general"

            d._deliver = track_deliver
            await d._drain_notification_queue()
            assert len(deliver_calls) == 0  # still outside hours

            remaining = d.db.conn.execute("SELECT * FROM pending_notifications").fetchall()
            assert len(remaining) == 1

            d.notify_hour_start = 0
            d.notify_hour_end = 24
            d._deliver = Dispatcher._deliver.__get__(d)
            d.http_session = FakeSession()
            await d._drain_notification_queue()

            remaining = d.db.conn.execute("SELECT * FROM pending_notifications").fetchall()
            assert len(remaining) == 0

        asyncio.run(run())
        os.unlink(tmpdb)


# --- Channel delivery retry ---
# Retry is built into _deliver: it tries 1 + len(CHANNEL_RETRY_DELAYS) times.

class TestChannelRetry:
    def test_retries_custom_channel_until_success(self):
        d, tmpdb = make_dispatcher()
        attempt_count = 0

        class FlakeyResp:
            status = 500
            async def text(self):
                return "error"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        class SuccessResp(FakeResp):
            pass

        class FlakeySession:
            def post(self, url, json=None, timeout=None):
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count < 3:
                    return FlakeyResp()
                return SuccessResp()

        async def run():
            d.http_session = FlakeySession()
            with patch("zo_dispatcher.server.asyncio.sleep", AsyncMock()):
                await d._deliver("discord/general", "Title", "Content")
                assert attempt_count == 3

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_exhausts_all_attempts(self):
        d, tmpdb = make_dispatcher()
        attempt_count = 0

        class AlwaysFailResp:
            status = 500
            async def text(self):
                return "error"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        class AlwaysFailSession:
            def post(self, url, json=None, timeout=None):
                nonlocal attempt_count
                attempt_count += 1
                return AlwaysFailResp()

        async def run():
            d.http_session = AlwaysFailSession()
            with patch("zo_dispatcher.server.asyncio.sleep", AsyncMock()):
                await d._deliver("discord/general", "Title", "Content")
                expected_attempts = 1 + len(CHANNEL_RETRY_DELAYS)
                assert attempt_count == expected_attempts

        asyncio.run(run())
        os.unlink(tmpdb)


# --- Notify levels ---

class TestNotifyLevels:
    def test_no_channel_silent_run(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(notify_channel=None, notify="always")
        notify_calls = []

        async def fake_zo_ask(*args, **kwargs):
            return "Agent output", "con_test"

        async def track_notify(*args, **kwargs):
            notify_calls.append(args)

        async def run():
            d.call_zo_ask = fake_zo_ask
            d._notify = track_notify
            await d.dispatch_agent(agent)
            assert len(notify_calls) == 0

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_notify_errors_no_notification_on_success(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(notify="errors", notify_channel="discord/general")
        notify_calls = []

        async def fake_zo_ask(*args, **kwargs):
            return "Agent output", "con_test"

        async def track_notify(*args, **kwargs):
            notify_calls.append(args)

        async def run():
            d.call_zo_ask = fake_zo_ask
            d._notify = track_notify
            await d.dispatch_agent(agent)
            assert len(notify_calls) == 0

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_notify_errors_sends_on_failure(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(notify="errors", notify_channel="discord/general")
        notify_calls = []

        async def fake_zo_ask_fail(*args, **kwargs):
            raise Exception("API error 500")

        async def track_notify(*args, **kwargs):
            notify_calls.append(kwargs if kwargs else args)

        async def run():
            d.call_zo_ask = fake_zo_ask_fail
            d._notify = track_notify
            await d.dispatch_agent(agent)
            assert len(notify_calls) == 1
            call = notify_calls[0]
            title = call.get("title", "") if isinstance(call, dict) else str(call)
            assert "[FAILED]" in title

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_notify_never_no_notifications(self):
        d, tmpdb = make_dispatcher()
        notify_calls = []

        async def fake_zo_ask(*args, **kwargs):
            return "Output", "con_test"

        async def fake_zo_ask_fail(*args, **kwargs):
            raise Exception("fail")

        async def track_notify(*args, **kwargs):
            notify_calls.append(("notify", args, kwargs))

        async def run():
            agent_ok = make_agent(notify="never", notify_channel="discord/general")
            d.call_zo_ask = fake_zo_ask
            d._notify = track_notify
            await d.dispatch_agent(agent_ok)
            assert len(notify_calls) == 0

            agent_fail = make_agent(notify="never", notify_channel="discord/general")
            d.call_zo_ask = fake_zo_ask_fail
            await d.dispatch_agent(agent_fail)
            assert len(notify_calls) == 0

        asyncio.run(run())
        os.unlink(tmpdb)
