"""Integration smoke tests for agent lifecycle limits (rate_limit, max_runs, expires_at).

These tests exercise the full dispatch→auto-disable→re-enable flows through
the Dispatcher class, as opposed to the unit tests in test_agents.py which
test individual methods in isolation.
"""

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from zo_dispatcher.agents import parse_agent_file, parse_rate_limit
from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.server import Dispatcher


# --- Helpers ---

def _make_dispatcher(tmp_path):
    """Create a Dispatcher with real DB but mocked-out networking."""
    with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
        d = Dispatcher.__new__(Dispatcher)
        d.db = DispatcherDB(str(tmp_path / "test.db"))
        d.config = {
            "system_notification_channel": "sms",
            "agents_dir": str(tmp_path / "agents"),
            "db_path": str(tmp_path / "test.db"),
        }
        d.agents_dir = tmp_path / "agents"
        d.agents_dir.mkdir(parents=True, exist_ok=True)
        d._agents = []
        d._max_runs_warned_at = {}
        d._last_parser_error_fingerprint = None
        d._last_parser_warning_fingerprint = None
        d._notify = AsyncMock()
        d._notify_lifecycle = AsyncMock()
        d.http_session = MagicMock()
        return d


def _write_agent(agents_dir, rel_path, content):
    p = agents_dir / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# --- max_runs: 1 (fire-once, replaces one_shot) ---

class TestMaxRunsFireOnce:
    """max_runs: 1 should fire exactly once, then auto-disable the agent file."""

    def test_dispatch_then_auto_disable(self, tmp_path):
        """After one successful dispatch, _handle_max_runs should trigger and
        _set_agent_active should write active: false to the file."""
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: One-Time Reminder
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=15"
max_runs: 1
notify_channel: sms
notify: always
active: true
---

Remind Jack about the release.
"""
        agent_file = _write_agent(d.agents_dir, "reminders/push.md", agent_content)

        agent, _ = parse_agent_file(agent_file, d.agents_dir)
        assert agent is not None
        assert agent["max_runs"] == 1

        # Simulate a dispatch: mark one run
        d.db.mark_run(agent["id"], status="success")

        # Now max_runs should be reached
        assert d._is_max_runs_reached(agent) is True

        # _set_agent_active should write active: false and record disabled_at
        d._set_agent_active(agent, False)

        content = agent_file.read_text()
        assert "active: false" in content
        assert "active: true" not in content
        assert d.db.get_disabled_at(agent["id"]) is not None

    def test_re_enable_resets_count(self, tmp_path):
        """After auto-disable from max_runs, re-enabling (active: true) should
        reset the run count via _check_re_enable."""
        d = _make_dispatcher(tmp_path)

        agent_id = "reminders/push"

        # Simulate: agent ran once, got disabled
        d.db.mark_run(agent_id, status="success")
        d.db.set_disabled_at(agent_id, datetime.now(timezone.utc))

        assert d.db.count_total_runs(agent_id) == 1
        assert d.db.get_disabled_at(agent_id) is not None

        # User re-enables the agent
        agent = {"id": agent_id, "active": True, "max_runs": 1}
        d._check_re_enable(agent)

        # Count should be reset
        assert d.db.count_total_runs(agent_id) == 0
        assert d.db.get_disabled_at(agent_id) is None

        # Agent should be able to fire again
        assert d._is_max_runs_reached(agent) is False


# --- max_runs: N (multi-fire lifecycle cap) ---

class TestMaxRunsMulti:
    """max_runs with N > 1 should allow exactly N dispatches."""

    def test_fires_n_times_then_disables(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent = {"id": "test/multi", "max_runs": 3, "active": True,
                 "_path": None, "notify": "always", "notify_channel": "sms"}

        # Fire 2 times — should not be reached
        for i in range(2):
            d.db.mark_run(agent["id"], status="success")
            assert d._is_max_runs_reached(agent) is False

        # Fire 3rd time — now reached
        d.db.mark_run(agent["id"], status="success")
        assert d._is_max_runs_reached(agent) is True

    def test_re_enable_gives_fresh_count(self, tmp_path):
        """After re-enable, the agent gets another full N dispatches."""
        d = _make_dispatcher(tmp_path)

        agent_id = "test/multi"
        max_runs = 3

        # Run to exhaustion
        for _ in range(max_runs):
            d.db.mark_run(agent_id, status="success")
        d.db.set_disabled_at(agent_id, datetime.now(timezone.utc))

        assert d.db.count_total_runs(agent_id) == 3

        # Re-enable
        agent = {"id": agent_id, "active": True, "max_runs": max_runs}
        d._check_re_enable(agent)

        assert d.db.count_total_runs(agent_id) == 0
        assert d._is_max_runs_reached(agent) is False

        # Can fire 3 more times
        for i in range(max_runs):
            d.db.mark_run(agent_id, status="success")
        assert d._is_max_runs_reached(agent) is True

    def test_lowering_max_runs_does_not_false_positive_re_enable(self, tmp_path):
        """If someone lowers max_runs below current count without re-enabling,
        _check_re_enable should NOT reset runs (no disabled_at = no re-enable)."""
        d = _make_dispatcher(tmp_path)

        agent_id = "test/lower"
        # Ran 5 times normally
        for _ in range(5):
            d.db.mark_run(agent_id, status="success")

        # User lowers max_runs to 3, agent is still active (no disabled_at)
        agent = {"id": agent_id, "active": True, "max_runs": 3}
        d._check_re_enable(agent)

        # Should NOT have cleared runs
        assert d.db.count_total_runs(agent_id) == 5
        # Should detect max_runs exceeded
        assert d._is_max_runs_reached(agent) is True


# --- expires_at ---

class TestExpiresAtIntegration:
    """Full flow: agent expires → auto-disable → re-enable with new date."""

    def test_expired_agent_auto_disables(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Conference Monitor
trigger: webhook
event: twitter.mention
expires_at: "2020-01-01T00:00:00+00:00"
notify_channel: discord/general
notify: always
active: true
---

Monitor mentions during the conference.
"""
        agent_file = _write_agent(d.agents_dir, "webhooks/conf.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        assert d._is_expired(agent) is True
        result = asyncio.run(d._handle_expiry(agent))
        assert result is True

        # File should now be disabled
        content = agent_file.read_text()
        assert "active: false" in content
        assert d.db.get_disabled_at(agent["id"]) is not None

    def test_future_agent_not_expired(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent = {
            "id": "test/future",
            "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
            "active": True,
            "notify": "always",
            "notify_channel": "sms",
        }

        result = asyncio.run(d._handle_expiry(agent))
        assert result is False

    def test_naive_expires_at_treated_as_utc(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Naive Expiry
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
expires_at: "2020-06-15T12:00:00"
active: true
---

Should be expired (naive = UTC).
"""
        agent_file = _write_agent(d.agents_dir, "schedules/naive.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        assert agent["expires_at"].tzinfo == timezone.utc
        assert d._is_expired(agent) is True


# --- rate_limit ---

class TestRateLimitIntegration:
    """rate_limit throttles without disabling. Agent stays active after window resets."""

    def test_drops_excess_triggers(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent = {"id": "test/rl", "rate_limit": "3/hour", "active": True}
        count, window = parse_rate_limit(agent["rate_limit"])

        # Fill up the window
        for _ in range(3):
            d.db.mark_run(agent["id"], status="success")

        runs = d.db.count_runs_in_window(agent["id"], window)
        assert runs >= count
        # Agent is NOT auto-disabled — still active
        assert d._is_max_runs_reached(agent) is False  # max_runs not set

    def test_rate_limit_resets_after_window(self, tmp_path):
        """Runs from outside the window shouldn't count."""
        d = _make_dispatcher(tmp_path)

        agent = {"id": "test/rl-reset", "rate_limit": "2/minute"}
        count, window = parse_rate_limit(agent["rate_limit"])

        # Record runs 2 minutes ago (outside window)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=window + 10)
        d.db.mark_run(agent["id"], status="success", dispatched_at=old_time)
        d.db.mark_run(agent["id"], status="success", dispatched_at=old_time)

        # Window should be clear
        runs = d.db.count_runs_in_window(agent["id"], window)
        assert runs == 0

    def test_rate_limit_and_max_runs_compose(self, tmp_path):
        """rate_limit: 5/hour + max_runs: 10 — throttle within window,
        auto-disable at lifetime cap."""
        d = _make_dispatcher(tmp_path)

        agent = {"id": "test/compose", "rate_limit": "5/hour", "max_runs": 10, "active": True}
        rl_count, rl_window = parse_rate_limit(agent["rate_limit"])

        # Fire 5 in current window — rate limited but not max_runs
        for _ in range(5):
            d.db.mark_run(agent["id"], status="success")

        assert d.db.count_runs_in_window(agent["id"], rl_window) >= rl_count
        assert d._is_max_runs_reached(agent) is False  # only 5 of 10

        # Fire 5 more from a different window (old timestamps)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=rl_window + 10)
        for _ in range(5):
            d.db.mark_run(agent["id"], status="success", dispatched_at=old_time)

        # Now total is 10 — max_runs reached
        assert d.db.count_total_runs(agent["id"]) == 10
        assert d._is_max_runs_reached(agent) is True


# --- Combined limits ---

class TestCombinedLimits:
    """When multiple limits are set, the first one reached wins."""

    def test_expires_at_wins_over_max_runs(self, tmp_path):
        """Agent with both expires_at (past) and max_runs (not reached) —
        expiry should trigger first."""
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Dual Limit
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
expires_at: "2020-01-01T00:00:00+00:00"
max_runs: 100
active: true
---

Should expire before max_runs.
"""
        agent_file = _write_agent(d.agents_dir, "schedules/dual.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        # No runs recorded — max_runs not reached, but expired
        assert d._is_max_runs_reached(agent) is False
        assert d._is_expired(agent) is True

        result = asyncio.run(d._handle_expiry(agent))
        assert result is True

    def test_max_runs_wins_over_expires_at(self, tmp_path):
        """Agent with both max_runs (reached) and expires_at (future) —
        max_runs should trigger."""
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Dual Limit 2
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
expires_at: "2099-01-01T00:00:00+00:00"
max_runs: 2
active: true
---

Should hit max_runs before expiry.
"""
        agent_file = _write_agent(d.agents_dir, "schedules/dual2.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        d.db.mark_run(agent["id"], status="success")
        d.db.mark_run(agent["id"], status="success")

        assert d._is_expired(agent) is False
        assert d._is_max_runs_reached(agent) is True

        result = asyncio.run(d._handle_max_runs(agent))
        assert result is True
        assert "active: false" in agent_file.read_text()


# --- Notification routing ---

class TestLifecycleNotifications:
    """Auto-disable notifications route to agent channel or system fallback."""

    def test_notifies_agent_channel(self, tmp_path):
        d = _make_dispatcher(tmp_path)
        # Restore real _notify_lifecycle but mock _notify
        d._notify_lifecycle = Dispatcher._notify_lifecycle.__get__(d, Dispatcher)
        d._notify = AsyncMock()

        agent = {
            "id": "test/notif",
            "notify": "always",
            "notify_channel": "discord/alerts",
        }

        asyncio.run(d._notify_lifecycle(agent, "Agent expired", "Agent test/notif expired."))
        d._notify.assert_called_once_with("discord/alerts", "Agent expired", "Agent test/notif expired.")

    def test_notify_never_stays_silent(self, tmp_path):
        """notify: never should produce no notification at all."""
        d = _make_dispatcher(tmp_path)
        d._notify_lifecycle = Dispatcher._notify_lifecycle.__get__(d, Dispatcher)
        d._notify = AsyncMock()

        agent = {
            "id": "test/notif2",
            "notify": "never",
            "notify_channel": "discord/general",
        }

        asyncio.run(d._notify_lifecycle(agent, "Run limit", "Disabled."))
        d._notify.assert_not_called()

    def test_notify_errors_skips_lifecycle(self, tmp_path):
        """notify: errors should not send lifecycle notifications (auto-disable
        is expected behavior, not an error)."""
        d = _make_dispatcher(tmp_path)
        d._notify_lifecycle = Dispatcher._notify_lifecycle.__get__(d, Dispatcher)
        d._notify = AsyncMock()

        agent = {
            "id": "test/notif-errors",
            "notify": "errors",
            "notify_channel": "discord/general",
        }

        asyncio.run(d._notify_lifecycle(agent, "Run limit", "Disabled."))
        d._notify.assert_not_called()

    def test_falls_back_when_no_channel(self, tmp_path):
        """notify: always + no notify_channel → fall back to system channel."""
        d = _make_dispatcher(tmp_path)
        d._notify_lifecycle = Dispatcher._notify_lifecycle.__get__(d, Dispatcher)
        d._notify = AsyncMock()

        agent = {
            "id": "test/notif3",
            "notify": "always",
            "notify_channel": None,
        }

        asyncio.run(d._notify_lifecycle(agent, "Expired", "Gone."))
        d._notify.assert_called_once_with("sms", "Expired", "Gone.")

    def test_errors_no_channel_stays_silent(self, tmp_path):
        """notify: errors + no notify_channel → no fallback, stays silent."""
        d = _make_dispatcher(tmp_path)
        d._notify_lifecycle = Dispatcher._notify_lifecycle.__get__(d, Dispatcher)
        d._notify = AsyncMock()

        agent = {
            "id": "test/notif4",
            "notify": "errors",
            "notify_channel": None,
        }

        asyncio.run(d._notify_lifecycle(agent, "Expired", "Gone."))
        d._notify.assert_not_called()


# --- Database: agent_state table ---

class TestAgentStateTable:
    """Smoke tests for the new agent_state table methods."""

    def test_set_and_get_disabled_at(self, tmp_path):
        db = DispatcherDB(str(tmp_path / "test.db"))
        now = datetime.now(timezone.utc)

        db.set_disabled_at("agent/a", now)
        result = db.get_disabled_at("agent/a")
        assert result is not None
        assert abs((result - now).total_seconds()) < 1

    def test_get_disabled_at_returns_none_when_unset(self, tmp_path):
        db = DispatcherDB(str(tmp_path / "test.db"))
        assert db.get_disabled_at("agent/nonexistent") is None

    def test_clear_disabled(self, tmp_path):
        db = DispatcherDB(str(tmp_path / "test.db"))
        db.set_disabled_at("agent/b", datetime.now(timezone.utc))
        assert db.get_disabled_at("agent/b") is not None

        db.clear_disabled("agent/b")
        assert db.get_disabled_at("agent/b") is None

    def test_clear_runs(self, tmp_path):
        db = DispatcherDB(str(tmp_path / "test.db"))
        for _ in range(5):
            db.mark_run("agent/c", status="success")
        assert db.count_total_runs("agent/c") == 5

        db.clear_runs("agent/c")
        assert db.count_total_runs("agent/c") == 0

    def test_clear_runs_only_affects_target_agent(self, tmp_path):
        db = DispatcherDB(str(tmp_path / "test.db"))
        for _ in range(3):
            db.mark_run("agent/x", status="success")
            db.mark_run("agent/y", status="success")

        db.clear_runs("agent/x")
        assert db.count_total_runs("agent/x") == 0
        assert db.count_total_runs("agent/y") == 3

    def test_set_disabled_at_upserts(self, tmp_path):
        """Setting disabled_at twice should overwrite, not duplicate."""
        db = DispatcherDB(str(tmp_path / "test.db"))
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)

        db.set_disabled_at("agent/d", t1)
        db.set_disabled_at("agent/d", t2)

        result = db.get_disabled_at("agent/d")
        assert abs((result - t2).total_seconds()) < 1


# --- Edge cases from PRD ---

class TestEdgeCases:
    """PRD edge case table."""

    def test_max_runs_zero_cannot_fire(self, tmp_path):
        d = _make_dispatcher(tmp_path)
        agent = {"id": "test/zero", "max_runs": 0, "active": True}
        assert d._is_max_runs_reached(agent) is True

    def test_already_inactive_expired_no_action(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Already Off
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
expires_at: "2020-01-01T00:00:00+00:00"
active: false
---

Already disabled.
"""
        agent_file = _write_agent(d.agents_dir, "schedules/off.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        # is_expired returns True but the tick/dispatch paths check active first
        assert agent["active"] is False
        assert d._is_expired(agent) is True

    def test_both_limits_set_first_wins(self, tmp_path):
        """When max_runs AND expires_at are both reached, whichever is checked
        first in the code path wins. Both should work independently."""
        d = _make_dispatcher(tmp_path)

        agent = {
            "id": "test/both",
            "max_runs": 1,
            "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "active": True,
        }
        d.db.mark_run(agent["id"], status="success")

        assert d._is_expired(agent) is True
        assert d._is_max_runs_reached(agent) is True


# --- Full dispatch cycle (mocked /zo/ask) ---

class TestFullDispatchCycle:
    """End-to-end: agent fires → mark_run → _handle_max_runs auto-disables."""

    def test_max_runs_1_full_cycle(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Fire Once
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
max_runs: 1
notify_channel: sms
notify: always
active: true
---

Do the thing once.
"""
        agent_file = _write_agent(d.agents_dir, "schedules/once.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        # Mock call_zo_ask to return a response
        d.call_zo_ask = AsyncMock(return_value=("Done!", "conv_123"))
        # Restore real _handle_max_runs
        d._handle_max_runs = lambda a: Dispatcher._handle_max_runs(d, a)

        asyncio.run(d.dispatch_agent(agent))

        # Verify the run was recorded
        assert d.db.count_total_runs(agent["id"]) == 1

        # Verify auto-disable happened
        content = agent_file.read_text()
        assert "active: false" in content
        assert d.db.get_disabled_at(agent["id"]) is not None

    def test_max_runs_3_fires_three_times(self, tmp_path):
        d = _make_dispatcher(tmp_path)

        agent_content = """\
---
title: Fire Three
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
max_runs: 3
notify_channel: sms
notify: always
active: true
---

Do the thing.
"""
        agent_file = _write_agent(d.agents_dir, "schedules/three.md", agent_content)
        agent, _ = parse_agent_file(agent_file, d.agents_dir)

        d.call_zo_ask = AsyncMock(return_value=("Done!", "conv_123"))
        d._handle_max_runs = lambda a: Dispatcher._handle_max_runs(d, a)

        # First two dispatches — should NOT auto-disable
        asyncio.run(d.dispatch_agent(agent))
        assert "active: true" in agent_file.read_text()

        asyncio.run(d.dispatch_agent(agent))
        assert "active: true" in agent_file.read_text()

        # Third dispatch — should auto-disable
        asyncio.run(d.dispatch_agent(agent))
        assert "active: false" in agent_file.read_text()
        assert d.db.count_total_runs(agent["id"]) == 3
