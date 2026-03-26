"""Tests for notification routing: custom channels, builtins, business hours, retry."""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zo_dispatcher.server import (
    Dispatcher,
    build_dispatcher_honcho_session_key,
    sanitize_honcho_key_component,
)
from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.channels import CHANNEL_RETRY_DELAYS, BUILTIN_CHANNELS


# --- Helpers ---

CONFIG = {
    "agents_dir": "/tmp/test-agents",
    "db_path": "",
    "zo_api_url": "https://api.zo.computer",
    "default_backend": "zo",
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
        "rate_limit": None,
        "max_runs": None,
        "expires_at": None,
        "honcho_session_scope": None,
        "prompt": "Do a thing.",
    }
    base.update(overrides)
    return base


class FakeResp:
    status = 200
    headers = {}
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


def make_hermes_session(captured: dict, body: dict | None = None, *, status: int = 200, text_body: str | None = None, headers: dict | None = None):
    response_body = body or {"output": "done", "conversation_id": "conv-1"}
    response_headers = headers or {}

    class HermesResp(FakeResp):
        async def json(self):
            return response_body

        async def text(self):
            if text_body is not None:
                return text_body
            return json.dumps(response_body)

    HermesResp.status = status
    HermesResp.headers = response_headers

    class HermesSession:
        def post(self, url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return HermesResp()

    return HermesSession()


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
                channel_config,
                "Test Title",
                "Test content",
                "general",
                "con_123",
                "con_123",
            )
            assert captured["url"] == "http://localhost:8787/notify"
            assert captured["json"]["channel_name"] == "general"
            assert captured["json"]["title"] == "Test Title"
            assert captured["json"]["content"] == "Test content"
            assert captured["json"]["conversation_id"] == "con_123"
            assert captured["json"]["honcho_session_key"] == "con_123"
            assert result["success"] is True

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_call_hermes_includes_hermes_fields(self):
        d, tmpdb = make_dispatcher()
        captured = {}

        async def run():
            d.http_session = make_hermes_session(captured)
            output, conv_id = await d.call_hermes(
                "Prompt",
                model="byok:test",
                timeout_seconds=30,
                reasoning_effort="high",
                max_iterations=9,
                skip_memory=True,
                skip_context=True,
                enabled_toolsets=["web", "file"],
                disabled_toolsets=["rl"],
                honcho_session_key="dispatcher-schedules-test-agent-7",
            )
            assert output == "done"
            assert conv_id == "conv-1"

        asyncio.run(run())
        payload = captured["json"]
        assert captured["url"] == "http://127.0.0.1:8788/ask"
        assert payload["model_name"] == "byok:test"
        assert payload["reasoning_effort"] == "high"
        assert payload["max_iterations"] == 9
        assert payload["skip_memory"] is True
        assert payload["skip_context"] is True
        assert payload["enabled_toolsets"] == ["web", "file"]
        assert payload["disabled_toolsets"] == ["rl"]
        assert payload["honcho_session_key"] == "dispatcher-schedules-test-agent-7"
        os.unlink(tmpdb)

    def test_call_hermes_omits_falsey_optional_fields(self):
        d, tmpdb = make_dispatcher()
        captured = {}

        async def run():
            d.http_session = make_hermes_session(captured)
            await d.call_hermes(
                "Prompt",
                skip_memory=False,
                skip_context=False,
                enabled_toolsets=[],
                disabled_toolsets=[],
            )

        asyncio.run(run())
        payload = captured["json"]
        assert "skip_memory" not in payload
        assert "skip_context" not in payload
        assert "enabled_toolsets" not in payload
        assert "disabled_toolsets" not in payload
        assert "honcho_session_key" not in payload
        os.unlink(tmpdb)

    def test_call_hermes_preserves_conversation_id_on_error(self):
        d, tmpdb = make_dispatcher()
        captured = {}

        async def run():
            d.http_session = make_hermes_session(
                captured,
                body={"error": "boom", "conversation_id": "conv-err"},
                status=500,
            )
            with pytest.raises(Exception) as excinfo:
                await d.call_hermes("Prompt")
            assert getattr(excinfo.value, "conv_id", "") == "conv-err"
            assert "Hermes API error 500" in str(excinfo.value)

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatcher_honcho_key_helpers(self):
        assert sanitize_honcho_key_component("pulse/ai-news") == "pulse-ai-news"
        assert build_dispatcher_honcho_session_key("pulse/ai-news", "per-agent", 12) == "dispatcher-pulse-ai-news"
        assert build_dispatcher_honcho_session_key("pulse/ai-news", "per-dispatch", 12) == "dispatcher-pulse-ai-news-12"

    def test_honcho_active_detection_uses_honcho_json(self, tmp_path, monkeypatch):
        d, tmpdb = make_dispatcher({"honcho_config_path": str(tmp_path / "honcho.json")})
        Path(d.config["honcho_config_path"]).write_text(json.dumps({
            "hosts": {"hermes": {"enabled": True}},
            "apiKey": "abc",
        }))
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)

        try:
            assert d._is_hermes_honcho_active() is True
        finally:
            os.unlink(tmpdb)

    def test_honcho_active_detection_false_without_config_or_env(self, tmp_path, monkeypatch):
        d, tmpdb = make_dispatcher({"honcho_config_path": str(tmp_path / "missing-honcho.json")})
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)

        try:
            assert d._is_hermes_honcho_active() is False
        finally:
            os.unlink(tmpdb)

    def test_dispatch_agent_passes_hermes_frontmatter(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(
            backend="hermes",
            reasoning="medium",
            max_iterations=5,
            skip_memory=True,
            skip_context=True,
            tools=["web", "terminal"],
            tools_deny=None,
        )

        async def run():
            with patch.object(d, "call_hermes", AsyncMock(return_value=("done", "conv-1"))) as call_hermes:
                with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                    d.db.begin_run = MagicMock(return_value=7)
                    d.db.finish_run = MagicMock()
                    await d.dispatch_agent(agent)
                    kwargs = call_hermes.await_args.kwargs
                    assert kwargs["reasoning_effort"] == "medium"
                    assert kwargs["max_iterations"] == 5
                    assert kwargs["skip_memory"] is True
                    assert kwargs["skip_context"] is True
                    assert kwargs["enabled_toolsets"] == ["web", "terminal"]
                    assert kwargs["disabled_toolsets"] is None
                    assert kwargs["honcho_session_key"] == "dispatcher-schedules-test-agent-7"

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatch_agent_uses_configured_default_hermes_backend(self):
        d, tmpdb = make_dispatcher({"default_backend": "hermes"})
        agent = make_agent(backend=None, model="byok:test")

        async def run():
            with patch.object(d, "call_hermes", AsyncMock(return_value=("done", "conv-1"))) as call_hermes:
                with patch.object(d, "call_zo_ask", AsyncMock()) as call_zo_ask:
                    with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                        d.db.begin_run = MagicMock(return_value=3)
                        d.db.finish_run = MagicMock()
                        await d.dispatch_agent(agent)
                        call_hermes.assert_awaited_once()
                        call_zo_ask.assert_not_called()
                        assert call_hermes.await_args.kwargs["honcho_session_key"] == "dispatcher-schedules-test-agent-3"

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatch_agent_defaults_to_zo_when_backend_omitted(self):
        d, tmpdb = make_dispatcher({"default_backend": "zo"})
        agent = make_agent(backend=None, model="byok:test")

        async def run():
            with patch.object(d, "call_zo_ask", AsyncMock(return_value=("done", "conv-1"))) as call_zo_ask:
                with patch.object(d, "call_hermes", AsyncMock()) as call_hermes:
                    d.db.begin_run = MagicMock(return_value=4)
                    d.db.finish_run = MagicMock()
                    await d.dispatch_agent(agent)
                    call_zo_ask.assert_awaited_once()
                    call_hermes.assert_not_called()
                    d.db.begin_run.assert_called_once()
                    d.db.finish_run.assert_called_once()
                    assert d.db.finish_run.call_args.args == (4,)
                    assert d.db.finish_run.call_args.kwargs["status"] == "success"
                    assert d.db.finish_run.call_args.kwargs["conv_id"] == "conv-1"
                    assert d.db.finish_run.call_args.kwargs["duration"] >= 0

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatch_agent_passes_tools_deny_and_omits_invalid_tool_fields(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(
            backend="hermes",
            tools="web",
            tools_deny=["browser"],
            skip_memory=False,
            skip_context=False,
        )

        async def run():
            with patch.object(d, "call_hermes", AsyncMock(return_value=("done", "conv-1"))) as call_hermes:
                with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                    d.db.begin_run = MagicMock(return_value=9)
                    d.db.finish_run = MagicMock()
                    await d.dispatch_agent(agent)
                    kwargs = call_hermes.await_args.kwargs
                    assert kwargs["enabled_toolsets"] is None
                    assert kwargs["disabled_toolsets"] == ["browser"]
                    assert kwargs["skip_memory"] is False
                    assert kwargs["skip_context"] is False
                    assert kwargs["honcho_session_key"] == "dispatcher-schedules-test-agent-9"

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatch_agent_uses_per_agent_honcho_key_across_dispatches(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(backend="hermes", honcho_session_scope="per-agent", notify="never")
        seen_keys = []

        async def fake_call_hermes(*args, **kwargs):
            seen_keys.append(kwargs["honcho_session_key"])
            return "done", f"conv-{len(seen_keys)}"

        async def run():
            d.call_hermes = fake_call_hermes
            with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                await d.dispatch_agent(agent)
                await d.dispatch_agent(agent)

        asyncio.run(run())
        assert seen_keys == [
            "dispatcher-schedules-test-agent",
            "dispatcher-schedules-test-agent",
        ]
        rows = d.db.conn.execute("SELECT id, status FROM agent_runs ORDER BY id").fetchall()
        assert len(rows) == 2
        assert [row["status"] for row in rows] == ["success", "success"]
        os.unlink(tmpdb)

    def test_dispatch_agent_defaults_to_per_dispatch_honcho_key_when_active(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(backend="hermes", notify="never")
        seen_keys = []

        async def fake_call_hermes(*args, **kwargs):
            seen_keys.append(kwargs["honcho_session_key"])
            return "done", f"conv-{len(seen_keys)}"

        async def run():
            d.call_hermes = fake_call_hermes
            with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                await d.dispatch_agent(agent)
                await d.dispatch_agent(agent)

        asyncio.run(run())
        assert seen_keys == [
            "dispatcher-schedules-test-agent-1",
            "dispatcher-schedules-test-agent-2",
        ]
        rows = d.db.conn.execute("SELECT id, status FROM agent_runs ORDER BY id").fetchall()
        assert len(rows) == 2
        assert [row["id"] for row in rows] == [1, 2]
        assert [row["status"] for row in rows] == ["success", "success"]
        os.unlink(tmpdb)

    def test_dispatch_agent_omits_honcho_key_when_honcho_inactive(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(backend="hermes", honcho_session_scope="per-agent", notify="never")

        async def run():
            with patch.object(d, "call_hermes", AsyncMock(return_value=("done", "conv-1"))) as call_hermes:
                with patch.object(d, "_is_hermes_honcho_active", return_value=False):
                    await d.dispatch_agent(agent)
                    assert call_hermes.await_args.kwargs["honcho_session_key"] is None

        asyncio.run(run())
        os.unlink(tmpdb)

    def test_dispatch_agent_creates_run_row_before_hermes_call(self):
        d, tmpdb = make_dispatcher()
        agent = make_agent(backend="hermes", notify="never")
        seen_rows = []

        async def fake_call_hermes(*args, **kwargs):
            rows = d.db.conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()
            seen_rows.append(dict(rows[0]))
            return "done", "conv-1"

        async def run():
            d.call_hermes = fake_call_hermes
            with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                await d.dispatch_agent(agent)

        asyncio.run(run())
        assert len(seen_rows) == 1
        assert seen_rows[0]["status"] == "started"
        assert seen_rows[0]["conv_id"] == ""
        final_row = d.db.conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert final_row["status"] == "success"
        assert final_row["conv_id"] == "conv-1"
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
                conv_id="conv-queued",
                honcho_session_key="dispatcher-schedules-test-agent-11",
            )

            pending = d.db.conn.execute("SELECT * FROM pending_notifications").fetchall()
            assert len(pending) == 1
            assert pending[0]["title"] == "[FAILED] Queued Test"
            assert pending[0]["channel_spec"] == "discord/general"
            assert pending[0]["conv_id"] == "conv-queued"
            assert pending[0]["honcho_session_key"] == "dispatcher-schedules-test-agent-11"

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

            assert len(d.http_session.calls) == 1
            assert d.http_session.calls[0]["json"]["conversation_id"] == "conv-queued"
            assert d.http_session.calls[0]["json"]["honcho_session_key"] == "dispatcher-schedules-test-agent-11"

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

    def test_hermes_notification_flow_uses_dispatcher_honcho_session_key(self):
        d, tmpdb = make_dispatcher({"default_backend": "hermes"})
        agent = make_agent(backend="hermes", notify="always", notify_channel="discord/general")
        notify_calls = []

        async def track_notify(*args, **kwargs):
            notify_calls.append(kwargs)

        async def run():
            d.call_hermes = AsyncMock(return_value=("Agent output", "conv-1"))
            d._notify = track_notify
            with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                await d.dispatch_agent(agent)

        asyncio.run(run())
        assert len(notify_calls) == 1
        assert notify_calls[0]["conv_id"] == "conv-1"
        assert notify_calls[0]["honcho_session_key"] == "dispatcher-schedules-test-agent-1"
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

    def test_hermes_failure_notifies_with_conversation_id_and_dispatcher_honcho_key(self):
        d, tmpdb = make_dispatcher({"default_backend": "hermes"})
        agent = make_agent(backend="hermes", notify="errors", notify_channel="discord/general")
        notify_calls = []

        async def fake_hermes_fail(*args, **kwargs):
            raise dmodule.ApiCallError(
                'Hermes API error 500: {"error":"boom","conversation_id":"conv-err"}',
                conv_id="conv-err",
            )

        async def track_notify(*args, **kwargs):
            notify_calls.append(kwargs)

        async def run():
            d.call_hermes = fake_hermes_fail
            d._notify = track_notify
            with patch.object(d, "_is_hermes_honcho_active", return_value=True):
                await d.dispatch_agent(agent)
            row = d.db.conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
            assert row["conv_id"] == "conv-err"
            assert row["status"] == "failure"
            assert len(notify_calls) == 1
            assert notify_calls[0]["conv_id"] == "conv-err"
            assert notify_calls[0]["honcho_session_key"] == "dispatcher-schedules-test-agent-1"
            assert "[FAILED]" in notify_calls[0]["title"]

        import zo_dispatcher.server as dmodule
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
