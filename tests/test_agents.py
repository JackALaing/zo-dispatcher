"""Tests for agent file parsing and scheduling."""

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from zo_dispatcher.agents import parse_agent_file, compute_next_run, parse_rate_limit
from zo_dispatcher.db import DispatcherDB
from zo_dispatcher.server import Dispatcher


# --- Fixtures ---

@pytest.fixture
def agents_dir(tmp_path):
    """Create a temporary agents directory with subdirectories."""
    sched = tmp_path / "schedules"
    sched.mkdir()
    return tmp_path


def write_agent(agents_dir, rel_path, content):
    """Write an agent file at agents_dir/rel_path."""
    p = agents_dir / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


VALID_SCHEDULE_AGENT = """\
---
title: Test Agent
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
model: byok:test-model
notify_channel: discord/general
notify: errors
active: true
---

Do the thing. Today is {{ date }}.
"""

VALID_WEBHOOK_AGENT = """\
---
title: Stripe Handler
trigger: webhook
event: stripe.checkout.session.completed
model: byok:test-model
notify_channel: discord/payments
notify: always
active: true
rate_limit: "10/hour"
---

Process the payment: {{ payload }}
"""

INACTIVE_AGENT = """\
---
title: Inactive Agent
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=6"
model: byok:other-model
notify_channel: sms
notify: never
active: false
---

This agent is inactive.
"""


# --- Parsing tests ---

class TestParseAgentFile:
    def test_valid_schedule_agent(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["id"] == "schedules/test-agent"
        assert agent["trigger"] == "schedule"
        assert agent["rrule"] == "RRULE:FREQ=DAILY;BYHOUR=12"
        assert agent["model"] == "byok:test-model"
        assert agent["notify_channel"] == "discord/general"
        assert agent["notify"] == "errors"
        assert agent["active"] is True
        assert agent["title"] == "Test Agent"
        assert "Do the thing" in agent["prompt"]

    def test_valid_webhook_agent(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/stripe.md", VALID_WEBHOOK_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["id"] == "webhooks/stripe"
        assert agent["trigger"] == "webhook"
        assert agent["event"] == ["stripe.checkout.session.completed"]
        assert agent["rate_limit"] == "10/hour"

    def test_inactive_agent(self, agents_dir):
        f = write_agent(agents_dir, "schedules/inactive.md", INACTIVE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["active"] is False

    def test_defaults(self, agents_dir):
        content = """\
---
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
---

Minimal agent.
"""
        f = write_agent(agents_dir, "schedules/minimal.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["active"] is True  # default
        assert agent["notify"] == "errors"  # default
        assert agent["model"] is None
        assert agent["persona"] is None
        assert agent["notify_channel"] is None
        assert agent["rate_limit"] is None
        assert agent["max_runs"] is None
        assert agent["expires_at"] is None

    def test_no_frontmatter(self, agents_dir):
        f = write_agent(agents_dir, "schedules/bad.md", "Just a markdown file.\n")
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert error == "No YAML frontmatter"

    def test_invalid_yaml(self, agents_dir):
        content = """\
---
trigger: [invalid yaml: {
---

Body text.
"""
        f = write_agent(agents_dir, "schedules/bad-yaml.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "Invalid YAML" in error

    def test_empty_body(self, agents_dir):
        content = """\
---
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
---
"""
        f = write_agent(agents_dir, "schedules/empty-body.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert error == "Empty prompt body"

    def test_missing_trigger(self, agents_dir):
        content = """\
---
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
---

Has body but no trigger.
"""
        f = write_agent(agents_dir, "schedules/no-trigger.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "trigger" in error.lower()

    def test_invalid_trigger(self, agents_dir):
        content = """\
---
trigger: cron
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
---

Invalid trigger type.
"""
        f = write_agent(agents_dir, "schedules/bad-trigger.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "cron" in error

    def test_schedule_missing_rrule(self, agents_dir):
        content = """\
---
trigger: schedule
---

Missing rrule.
"""
        f = write_agent(agents_dir, "schedules/no-rrule.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "rrule" in error.lower()

    def test_webhook_missing_event(self, agents_dir):
        content = """\
---
trigger: webhook
---

Missing event.
"""
        f = write_agent(agents_dir, "webhooks/no-event.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "event" in error.lower()

    def test_namespaced_id_from_path(self, agents_dir):
        f = write_agent(agents_dir, "deep/nested/agent.md", """\
---
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
---

Deeply nested.
""")
        agent, error = parse_agent_file(f, agents_dir)
        assert error is None
        assert agent["id"] == "deep/nested/agent"

    def test_hermes_frontmatter_fields(self, agents_dir):
        f = write_agent(agents_dir, "schedules/hermes.md", """\
---
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
backend: hermes
reasoning: high
max_iterations: 12
skip_memory: true
skip_context: true
tools: [web, file]
---

Hermes agent body.
""")
        agent, error = parse_agent_file(f, agents_dir)
        assert error is None
        assert agent["backend"] == "hermes"
        assert agent["reasoning"] == "high"
        assert agent["max_iterations"] == 12
        assert agent["skip_memory"] is True
        assert agent["skip_context"] is True
        assert agent["tools"] == ["web", "file"]

    def test_tools_and_tools_deny_are_mutually_exclusive(self, agents_dir):
        f = write_agent(agents_dir, "schedules/bad-tools.md", """\
---
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=0"
tools: [web]
tools_deny: [rl]
---

Conflicting tool config.
""")
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "mutually exclusive" in error


# --- Schedule computation tests ---
# dtstart is deliberately naive — tz-awareness comes from the rrule itself if needed.

class TestComputeNextRun:
    def test_daily_rrule(self):
        after = datetime(2026, 3, 1, 13, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("RRULE:FREQ=DAILY;BYHOUR=12", after)
        assert next_run is not None
        assert next_run > after
        assert next_run.hour == 12

    def test_hourly_rrule(self):
        after = datetime(2026, 3, 1, 10, 30, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("RRULE:FREQ=HOURLY;BYMINUTE=0", after)
        assert next_run is not None
        assert next_run > after
        assert next_run.minute == 0

    def test_weekly_rrule(self):
        after = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("RRULE:FREQ=WEEKLY;BYDAY=SU;BYHOUR=3", after)
        assert next_run is not None
        assert next_run > after

    def test_next_run_after_last(self):
        after = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("RRULE:FREQ=DAILY;BYHOUR=12", after)
        assert next_run is not None
        assert next_run > after

    def test_naive_datetime_gets_utc(self):
        after = datetime(2026, 3, 1, 12, 0, 0)
        next_run = compute_next_run("RRULE:FREQ=DAILY;BYHOUR=12", after)
        assert next_run is not None
        assert next_run.tzinfo is not None

    def test_invalid_rrule(self):
        after = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("not a valid rrule", after)
        assert next_run is None

    def test_result_always_has_timezone(self):
        after = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run("RRULE:FREQ=DAILY;BYHOUR=6", after)
        assert next_run is not None
        assert next_run.tzinfo is not None


# --- Duplicate ID detection ---

class TestDuplicateDetection:
    def test_single_agent(self, agents_dir):
        write_agent(agents_dir, "schedules/test.md", VALID_SCHEDULE_AGENT)

        agents = []
        for f in sorted(agents_dir.rglob("*.md")):
            agent, error = parse_agent_file(f, agents_dir)
            if agent:
                agents.append(agent)

        assert len(agents) == 1

    def test_duplicate_ids_both_removed(self):
        """Duplicate detection logic: both copies should be removed."""
        agents_list = []
        seen_ids = {}

        agent1 = {"id": "schedules/dup", "filepath": "/a"}
        agent2 = {"id": "schedules/dup", "filepath": "/b"}

        for agent in [agent1, agent2]:
            if agent["id"] in seen_ids:
                agents_list = [a for a in agents_list if a["id"] != agent["id"]]
            else:
                seen_ids[agent["id"]] = agent["filepath"]
                agents_list.append(agent)

        assert len(agents_list) == 0


# --- Template variable injection ---

class TestTemplateVariables:
    def _make_dispatcher(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            return Dispatcher.__new__(Dispatcher)

    def test_date_and_timestamp_replaced(self):
        d = self._make_dispatcher()
        agent = {
            "id": "schedules/test",
            "prompt": "Today is {{ date }}. Time: {{ timestamp }}.",
        }
        result = d._prepare_prompt(agent)
        assert "{{ date }}" not in result
        assert "{{ timestamp }}" not in result

    def test_agent_id_replaced(self):
        d = self._make_dispatcher()
        agent = {"id": "schedules/my-agent", "prompt": "Agent: {{ agent_id }}."}
        result = d._prepare_prompt(agent)
        assert "schedules/my-agent" in result

    def test_webhook_context_variables(self):
        d = self._make_dispatcher()
        agent = {
            "id": "webhooks/stripe",
            "prompt": "Event: {{ event_type }}. Payload: {{ payload }}.",
        }
        context = {
            "payload": {"amount": 100},
            "headers": {},
            "event_type": "checkout.session.completed",
        }
        result = d._prepare_prompt(agent, context)
        assert "checkout.session.completed" in result
        assert '"amount": 100' in result
        assert "{{ event_type }}" not in result
        assert "{{ payload }}" not in result

    def test_no_context_leaves_webhook_templates(self):
        d = self._make_dispatcher()
        agent = {"id": "test/x", "prompt": "Payload: {{ payload }}."}
        result = d._prepare_prompt(agent)
        assert "{{ payload }}" in result


# --- Dual-trigger (trigger: both) tests ---

VALID_BOTH_AGENT = """\
---
title: System Heartbeat
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: monitoring
model: byok:test-model
notify_channel: discord/general
notify: always
rate_limit: "20/hour"
active: true
---

Check system health. {{ payload }}
"""


class TestDualTriggerParsing:
    def test_valid_both_agent(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/system.md", VALID_BOTH_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["id"] == "heartbeats/system"
        assert agent["trigger"] == "both"
        assert agent["rrule"] == "RRULE:FREQ=MINUTELY;INTERVAL=15"
        assert agent["event"] == ["monitoring"]
        assert agent["rate_limit"] == "20/hour"

    def test_both_missing_rrule(self, agents_dir):
        content = """\
---
title: Bad Both
trigger: both
event: monitoring
---

Missing rrule.
"""
        f = write_agent(agents_dir, "heartbeats/no-rrule.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "Dual-trigger agent missing rrule" == error

    def test_both_missing_event(self, agents_dir):
        content = """\
---
title: Bad Both
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
---

Missing event.
"""
        f = write_agent(agents_dir, "heartbeats/no-event.md", content)
        agent, error = parse_agent_file(f, agents_dir)
        assert agent is None
        assert "Dual-trigger agent missing event" == error


class TestDualTriggerScheduling:
    def test_is_due_returns_true_for_both_agent(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.db = type("MockDB", (), {
                "get_last_run": lambda self, agent_id: datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
                "mark_run": lambda self, *a, **kw: None,
                "get_disabled_at": lambda self, agent_id: None,
            })()

        agent = {
            "id": "heartbeats/system",
            "trigger": "both",
            "rrule": "RRULE:FREQ=MINUTELY;INTERVAL=15",
            "active": True,
            "max_runs": None,
        }
        with patch("zo_dispatcher.server.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 1, 1, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = d.is_due(agent)
        assert result is True

    def test_is_due_returns_false_for_webhook_only(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.db = type("MockDB", (), {
                "get_last_run": lambda self, agent_id: datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
                "get_disabled_at": lambda self, agent_id: None,
            })()

        agent = {
            "id": "webhooks/test",
            "trigger": "webhook",
            "active": True,
            "max_runs": None,
        }
        result = d.is_due(agent)
        assert result is False


class TestDualTriggerWebhookMatching:
    def test_active_webhook_agents_includes_both(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d._agents = [
                {"id": "a", "trigger": "schedule", "active": True},
                {"id": "b", "trigger": "webhook", "active": True},
                {"id": "c", "trigger": "both", "active": True},
                {"id": "d", "trigger": "both", "active": False},
            ]
        result = d._active_webhook_agents()
        ids = [a["id"] for a in result]
        assert "b" in ids
        assert "c" in ids
        assert "a" not in ids
        assert "d" not in ids


# --- Deferred webhooks ---

VALID_DEFER_AGENT = """\
---
title: Todoist Inbox Triage
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: todoist.item
defer_to_cron: skip_if_empty
notify_channel: discord/todoist
notify: always
---

Process all queued events from {{ queue_file }}.
"""

VALID_ALWAYS_RUN_AGENT = """\
---
title: Todoist Inbox + Enrichment
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: todoist.item
defer_to_cron: always_run
notify_channel: discord/todoist
notify: always
---

{% if queue_file %}
New events to process:
{{ queue_file }}
{% endif %}

Now do your regular routine work.
"""

DEFER_NO_QUEUE_FILE = """\
---
title: Missing Queue File Ref
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: todoist.item
defer_to_cron: skip_if_empty
---

Process events but forgot the queue_file variable.
"""

DEFER_SCHEDULE_ONLY = """\
---
title: Bad Defer Schedule
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=6"
defer_to_cron: skip_if_empty
---

This should fail.
"""

DEFER_WEBHOOK_ONLY = """\
---
title: Bad Defer Webhook
trigger: webhook
event: todoist.item
defer_to_cron: skip_if_empty
---

This should fail.
"""

DEFER_INVALID_VALUE = """\
---
title: Bad Defer Value
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: todoist.item
defer_to_cron: banana
---

This should fail.
"""


class TestDeferParsing:
    def test_skip_if_empty_with_both_parses(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/triage.md", VALID_DEFER_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["defer_to_cron"] == "skip_if_empty"
        assert agent["trigger"] == "both"

    def test_always_run_with_both_parses(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/enriched.md", VALID_ALWAYS_RUN_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["defer_to_cron"] == "always_run"
        assert agent["trigger"] == "both"

    def test_invalid_value_rejected(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/bad-value.md", DEFER_INVALID_VALUE)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "defer_to_cron must be" in error
        assert "banana" in error

    def test_defer_to_cron_with_schedule_rejected(self, agents_dir):
        f = write_agent(agents_dir, "schedules/bad-defer.md", DEFER_SCHEDULE_ONLY)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert error == "defer_to_cron requires trigger: both (no events to queue)"

    def test_defer_to_cron_with_webhook_rejected(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/bad-defer.md", DEFER_WEBHOOK_ONLY)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert error == "defer_to_cron requires trigger: both (no scheduled run to drain queue)"

    def test_defer_warns_missing_queue_file(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/no-qf.md", DEFER_NO_QUEUE_FILE)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["defer_to_cron"] == "skip_if_empty"
        assert len(agent["_warnings"]) == 1
        assert "queue_file" in agent["_warnings"][0]

    def test_defer_to_cron_false_default(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["defer_to_cron"] is False

    def test_bool_true_treated_as_skip_if_empty(self, agents_dir):
        content = """\
---
title: Legacy Bool True
trigger: both
rrule: "RRULE:FREQ=MINUTELY;INTERVAL=15"
event: todoist.item
defer_to_cron: true
---

Process events from {{ queue_file }}.
"""
        f = write_agent(agents_dir, "heartbeats/legacy.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["defer_to_cron"] == "skip_if_empty"


class TestDeferredQueueOperations:
    def _make_dispatcher(self, agents_dir):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.agents_dir = agents_dir
            d._queue_locks = {}
            return d

    def test_append_deferred_event(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "todoist.item.added", {"task": "Buy milk"})
        )

        qpath = d._queue_path("heartbeats/triage")
        assert qpath.exists()
        lines = [l for l in qpath.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "todoist.item.added"
        assert entry["payload"]["task"] == "Buy milk"
        assert "received_at" in entry

    def test_append_multiple_events(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for i in range(3):
            asyncio.run(
                d._append_deferred_event("heartbeats/triage", f"event.{i}", {"i": i})
            )

        qpath = d._queue_path("heartbeats/triage")
        lines = [l for l in qpath.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_count_deferred_events(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        assert d._count_deferred_events("heartbeats/triage") == 0
        for i in range(5):
            asyncio.run(
                d._append_deferred_event("heartbeats/triage", f"event.{i}", {"i": i})
            )

        assert d._count_deferred_events("heartbeats/triage") == 5

    def test_snapshot_queue(self, tmp_path):
        d = self._make_dispatcher(tmp_path)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.1", {"a": 1})
        )
        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.2", {"a": 2})
        )

        snapshot = d._snapshot_queue("heartbeats/triage")
        assert snapshot is not None
        assert snapshot.exists()

        lines = [l for l in snapshot.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

        qpath = d._queue_path("heartbeats/triage")
        assert not qpath.exists()

    def test_snapshot_returns_none_when_empty(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        snapshot = d._snapshot_queue("heartbeats/triage")
        assert snapshot is None

    def test_new_events_go_to_fresh_queue_after_snapshot(self, tmp_path):
        d = self._make_dispatcher(tmp_path)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.1", {"a": 1})
        )

        snapshot = d._snapshot_queue("heartbeats/triage")
        assert snapshot is not None

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.2", {"a": 2})
        )

        qpath = d._queue_path("heartbeats/triage")
        assert qpath.exists()
        new_lines = [l for l in qpath.read_text().splitlines() if l.strip()]
        assert len(new_lines) == 1

        snap_lines = [l for l in snapshot.read_text().splitlines() if l.strip()]
        assert len(snap_lines) == 1

    def test_cleanup_snapshot_on_success(self, tmp_path):
        d = self._make_dispatcher(tmp_path)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.1", {"a": 1})
        )
        snapshot = d._snapshot_queue("heartbeats/triage")
        assert snapshot.exists()

        d._cleanup_snapshot(snapshot, success=True)
        assert not snapshot.exists()

    def test_cleanup_snapshot_preserved_on_failure(self, tmp_path):
        d = self._make_dispatcher(tmp_path)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.1", {"a": 1})
        )
        snapshot = d._snapshot_queue("heartbeats/triage")
        assert snapshot.exists()

        d._cleanup_snapshot(snapshot, success=False)
        assert snapshot.exists()

    def test_leftover_snapshots_merged(self, tmp_path):
        d = self._make_dispatcher(tmp_path)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.old", {"old": True})
        )
        old_snapshot = d._snapshot_queue("heartbeats/triage")
        d._cleanup_snapshot(old_snapshot, success=False)

        asyncio.run(
            d._append_deferred_event("heartbeats/triage", "event.new", {"new": True})
        )

        merged_snapshot = d._snapshot_queue("heartbeats/triage")
        assert merged_snapshot is not None
        lines = [l for l in merged_snapshot.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

        events = [json.loads(l) for l in lines]
        event_types = {e["event_type"] for e in events}
        assert "event.old" in event_types
        assert "event.new" in event_types

        qdir = d._queues_dir()
        remaining = list(qdir.glob(d._snapshot_glob("heartbeats/triage")))
        assert len(remaining) == 1
        assert remaining[0] == merged_snapshot


class TestDeferredTemplateVariable:
    def _make_dispatcher(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            return Dispatcher.__new__(Dispatcher)

    def test_queue_file_replaced(self):
        d = self._make_dispatcher()
        agent = {
            "id": "heartbeats/triage",
            "prompt": "Process events from {{ queue_file }}.",
        }
        result = d._prepare_prompt(agent, queue_file=Path("/tmp/test-snapshot.jsonl"))
        assert "/tmp/test-snapshot.jsonl" in result
        assert "{{ queue_file }}" not in result

    def test_queue_file_not_replaced_without_path(self):
        d = self._make_dispatcher()
        agent = {
            "id": "heartbeats/triage",
            "prompt": "Process events from {{ queue_file }}.",
        }
        result = d._prepare_prompt(agent)
        assert "{{ queue_file }}" in result

    def test_queue_file_message_for_always_run(self):
        d = self._make_dispatcher()
        agent = {
            "id": "heartbeats/triage",
            "prompt": "Events: {{ queue_file }}\nDo routine.",
        }
        result = d._prepare_prompt(agent, queue_file="No events queued.")
        assert "{{ queue_file }}" not in result
        assert "No events queued." in result

    def test_deferred_event_not_counted_against_rate_limit(self, tmp_path):
        """Deferred events should not count against rate_limit since no LLM call is made."""
        d = self._make_dispatcher()
        d.agents_dir = tmp_path
        d._queue_locks = {}

        agent = {
            "id": "heartbeats/triage",
            "trigger": "both",
            "defer_to_cron": "skip_if_empty",
            "rate_limit": "5/hour",
        }
        for i in range(10):
            asyncio.run(
                d._append_deferred_event(agent["id"], f"event.{i}", {"i": i})
            )

        assert d._count_deferred_events(agent["id"]) == 10


# --- Multi-event parsing ---

MULTI_EVENT_AGENT = """\
---
title: Activity Digest
trigger: both
rrule: "RRULE:FREQ=DAILY;BYHOUR=18;BYMINUTE=0"
event:
  - github.push
  - github.pull_request
  - linear.issue
  - todoist.item
defer_to_cron: skip_if_empty
notify_channel: discord/general
notify: always
---

Read all queued events from {{ queue_file }}.
Group events by source and summarize activity.
"""

MULTI_EVENT_WEBHOOK_ONLY = """\
---
title: Multi-Event Webhook
trigger: webhook
event:
  - stripe.checkout
  - stripe.payment_intent.succeeded
notify_channel: discord/payments
notify: always
max_runs: 10
---

Process the payment: {{ payload }}
"""


class TestMultiEventParsing:
    def test_list_event_parsed(self, agents_dir):
        f = write_agent(agents_dir, "digests/activity.md", MULTI_EVENT_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert isinstance(agent["event"], list)
        assert len(agent["event"]) == 4
        assert "github.push" in agent["event"]
        assert "todoist.item" in agent["event"]

    def test_single_event_normalized_to_list(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/stripe.md", VALID_WEBHOOK_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert isinstance(agent["event"], list)
        assert len(agent["event"]) == 1
        assert agent["event"][0] == "stripe.checkout.session.completed"

    def test_empty_event_list_rejected(self, agents_dir):
        content = """\
---
title: Empty Events
trigger: webhook
event: []
---

Should fail.
"""
        f = write_agent(agents_dir, "webhooks/empty.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "must not be empty" in error

    def test_mixed_types_in_list_rejected(self, agents_dir):
        content = """\
---
title: Bad Events
trigger: webhook
event:
  - github.push
  - 123
---

Should fail.
"""
        f = write_agent(agents_dir, "webhooks/mixed.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "non-empty strings" in error

    def test_empty_string_in_list_rejected(self, agents_dir):
        content = """\
---
title: Bad Events
trigger: webhook
event:
  - github.push
  - ""
---

Should fail.
"""
        f = write_agent(agents_dir, "webhooks/empty-str.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "non-empty strings" in error

    def test_multi_event_webhook_only(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/multi-stripe.md", MULTI_EVENT_WEBHOOK_ONLY)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert len(agent["event"]) == 2

    def test_schedule_agent_event_is_none(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["event"] is None

    def test_multi_event_deferred_agent(self, agents_dir):
        f = write_agent(agents_dir, "digests/activity.md", MULTI_EVENT_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["defer_to_cron"] == "skip_if_empty"
        assert len(agent["event"]) == 4


# --- Lifecycle limits ---

MAX_RUNS_SCHEDULE = """\
---
title: Push Reminder
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=15"
max_runs: 1
active: true
notify_channel: sms
notify: always
---

Remind Jack to push the release branch.
"""

MAX_RUNS_WEBHOOK = """\
---
title: One-Time Webhook
trigger: webhook
event: github.release
max_runs: 1
active: true
notify_channel: discord/general
notify: always
---

Process the release: {{ payload }}
"""

MAX_RUNS_BOTH = """\
---
title: One-Time Both
trigger: both
rrule: "RRULE:FREQ=DAILY;BYHOUR=12"
event: monitoring
max_runs: 1
active: true
---

Check once then stop.
"""

EXPIRES_AGENT = """\
---
title: Temp Monitor
trigger: schedule
rrule: "RRULE:FREQ=HOURLY"
expires_at: "2026-03-20T00:00:00"
active: true
---

Check the thing.
"""

RATE_LIMIT_AGENT = """\
---
title: Rate Limited
trigger: webhook
event: github.push
rate_limit: "5/minute"
active: true
---

Handle push: {{ payload }}
"""


class TestLifecycleParsing:
    def test_max_runs_parsed(self, agents_dir):
        f = write_agent(agents_dir, "reminders/push.md", MAX_RUNS_SCHEDULE)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["max_runs"] == 1
        assert agent["active"] is True

    def test_max_runs_default_none(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["max_runs"] is None

    def test_max_runs_webhook(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/one-time.md", MAX_RUNS_WEBHOOK)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["max_runs"] == 1
        assert agent["trigger"] == "webhook"

    def test_max_runs_both(self, agents_dir):
        f = write_agent(agents_dir, "heartbeats/one-time.md", MAX_RUNS_BOTH)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["max_runs"] == 1
        assert agent["trigger"] == "both"

    def test_path_stored(self, agents_dir):
        f = write_agent(agents_dir, "reminders/push.md", MAX_RUNS_SCHEDULE)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["_path"] == str(f)

    def test_expires_at_parsed(self, agents_dir):
        f = write_agent(agents_dir, "schedules/temp.md", EXPIRES_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent is not None
        assert agent["expires_at"] == datetime(2026, 3, 20, 0, 0, 0, tzinfo=timezone.utc)

    def test_expires_at_default_none(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["expires_at"] is None

    def test_expires_at_with_timezone(self, agents_dir):
        content = """\
---
title: TZ Expiry
trigger: schedule
rrule: "RRULE:FREQ=HOURLY"
expires_at: "2026-03-20T00:00:00+05:00"
active: true
---

Check.
"""
        f = write_agent(agents_dir, "schedules/tz.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["expires_at"].tzinfo is not None

    def test_invalid_expires_at_rejected(self, agents_dir):
        content = """\
---
title: Bad Expiry
trigger: schedule
rrule: "RRULE:FREQ=HOURLY"
expires_at: "not-a-date"
active: true
---

Check.
"""
        f = write_agent(agents_dir, "schedules/bad-exp.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "expires_at" in error

    def test_rate_limit_parsed(self, agents_dir):
        f = write_agent(agents_dir, "webhooks/rl.md", RATE_LIMIT_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["rate_limit"] == "5/minute"

    def test_rate_limit_default_none(self, agents_dir):
        f = write_agent(agents_dir, "schedules/test-agent.md", VALID_SCHEDULE_AGENT)
        agent, error = parse_agent_file(f, agents_dir)

        assert error is None
        assert agent["rate_limit"] is None

    def test_invalid_rate_limit_rejected(self, agents_dir):
        content = """\
---
title: Bad RL
trigger: webhook
event: test
rate_limit: "10/fortnight"
active: true
---

Check.
"""
        f = write_agent(agents_dir, "webhooks/bad-rl.md", content)
        agent, error = parse_agent_file(f, agents_dir)

        assert agent is None
        assert "rate_limit" in error.lower()


class TestParseRateLimit:
    def test_per_minute(self):
        count, window = parse_rate_limit("5/minute")
        assert count == 5
        assert window == 60

    def test_per_hour(self):
        count, window = parse_rate_limit("10/hour")
        assert count == 10
        assert window == 3600

    def test_per_day(self):
        count, window = parse_rate_limit("50/day")
        assert count == 50
        assert window == 86400

    def test_invalid_unit(self):
        with pytest.raises(ValueError, match="unit"):
            parse_rate_limit("10/fortnight")

    def test_invalid_count(self):
        with pytest.raises(ValueError, match="count"):
            parse_rate_limit("abc/hour")

    def test_no_slash(self):
        with pytest.raises(ValueError, match="format"):
            parse_rate_limit("10hour")

    def test_negative_count(self):
        with pytest.raises(ValueError, match="non-negative"):
            parse_rate_limit("-1/hour")


class TestSetAgentActive:
    def _make_dispatcher(self, tmp_path):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.db = DispatcherDB(str(tmp_path / "test.db"))
            return d

    def test_deactivate_agent(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent_file = tmp_path / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(MAX_RUNS_SCHEDULE)

        agent = {"id": "reminders/push", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, False)

        content = agent_file.read_text()
        assert "active: false" in content
        assert "active: true" not in content
        assert d.db.get_disabled_at("reminders/push") is not None

    def test_reactivate_agent(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent_file = tmp_path / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(MAX_RUNS_SCHEDULE.replace("active: true", "active: false"))

        agent = {"id": "reminders/push", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, True)

        content = agent_file.read_text()
        assert "active: true" in content
        assert "active: false" not in content

    def test_insert_active_when_absent(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent_content = """\
---
title: No Active Field
trigger: schedule
rrule: "RRULE:FREQ=DAILY;BYHOUR=15"
max_runs: 1
---

Do the thing.
"""
        agent_file = tmp_path / "reminders" / "no-active.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(agent_content)

        agent = {"id": "reminders/no-active", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, False)

        content = agent_file.read_text()
        assert "active: false" in content

    def test_body_preserved(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent_file = tmp_path / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(MAX_RUNS_SCHEDULE)

        agent = {"id": "reminders/push", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, False)

        content = agent_file.read_text()
        assert "Remind Jack to push the release branch." in content
        assert "max_runs: 1" in content
        assert "title: Push Reminder" in content

    def test_atomic_write(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent_file = tmp_path / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(MAX_RUNS_SCHEDULE)

        agent = {"id": "reminders/push", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, False)

        assert not agent_file.with_suffix(".tmp").exists()

    def test_already_inactive_is_noop(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        inactive = MAX_RUNS_SCHEDULE.replace("active: true", "active: false")
        agent_file = tmp_path / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(inactive)

        agent = {"id": "reminders/push", "_path": str(agent_file), "max_runs": 1}
        d._set_agent_active(agent, False)

        content = agent_file.read_text()
        assert content.count("active: false") == 1

    def test_refresh_picks_up_deactivation(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_file = agents_dir / "reminders" / "push.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text(MAX_RUNS_SCHEDULE)

        d = self._make_dispatcher(tmp_path)
        d.agents_dir = agents_dir
        d._last_parser_error_fingerprint = None
        d._last_parser_warning_fingerprint = None
        d.config = {"system_notification_channel": None}

        agents = d.scan_agents()
        assert len(agents) == 1
        assert agents[0]["active"] is True

        d._set_agent_active(agents[0], False)

        agents = d.scan_agents()
        assert len(agents) == 1
        assert agents[0]["active"] is False


# --- Lifecycle behavior tests ---

class TestExpiry:
    def _make_dispatcher(self):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            return Dispatcher.__new__(Dispatcher)

    def test_not_expired_when_unset(self):
        d = self._make_dispatcher()
        agent = {"id": "test/a", "expires_at": None}
        assert d._is_expired(agent) is False

    def test_expired_in_past(self):
        d = self._make_dispatcher()
        agent = {"id": "test/a", "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}
        assert d._is_expired(agent) is True

    def test_not_expired_in_future(self):
        d = self._make_dispatcher()
        agent = {"id": "test/a", "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc)}
        assert d._is_expired(agent) is False

    def test_expired_string_in_past(self):
        d = self._make_dispatcher()
        agent = {"id": "test/a", "expires_at": "2020-01-01T00:00:00"}
        assert d._is_expired(agent) is True

    def test_naive_datetime_assumed_utc(self):
        d = self._make_dispatcher()
        agent = {"id": "test/a", "expires_at": datetime(2020, 1, 1)}
        assert d._is_expired(agent) is True


class TestMaxRunsReached:
    def _make_dispatcher(self, tmp_path):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.db = DispatcherDB(str(tmp_path / "test.db"))
            return d

    def test_not_reached_when_unset(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent = {"id": "test/a", "max_runs": None}
        assert d._is_max_runs_reached(agent) is False

    def test_not_reached_below_limit(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        d.db.mark_run("test/a", status="success")
        agent = {"id": "test/a", "max_runs": 5}
        assert d._is_max_runs_reached(agent) is False

    def test_reached_at_limit(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for _ in range(5):
            d.db.mark_run("test/a", status="success")
        agent = {"id": "test/a", "max_runs": 5}
        assert d._is_max_runs_reached(agent) is True

    def test_reached_above_limit(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for _ in range(10):
            d.db.mark_run("test/a", status="success")
        agent = {"id": "test/a", "max_runs": 5}
        assert d._is_max_runs_reached(agent) is True

    def test_max_runs_zero_immediately_reached(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        agent = {"id": "test/a", "max_runs": 0}
        assert d._is_max_runs_reached(agent) is True


class TestReEnable:
    def _make_dispatcher(self, tmp_path):
        with patch.object(Dispatcher, '__init__', lambda self, *a, **kw: None):
            d = Dispatcher.__new__(Dispatcher)
            d.db = DispatcherDB(str(tmp_path / "test.db"))
            return d

    def test_re_enable_clears_runs(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for _ in range(5):
            d.db.mark_run("test/a", status="success")
        d.db.set_disabled_at("test/a", datetime.now(timezone.utc))

        agent = {"id": "test/a", "active": True, "max_runs": 5}
        d._check_re_enable(agent)

        assert d.db.count_total_runs("test/a") == 0
        assert d.db.get_disabled_at("test/a") is None

    def test_no_re_enable_without_disabled_at(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for _ in range(5):
            d.db.mark_run("test/a", status="success")

        agent = {"id": "test/a", "active": True, "max_runs": 5}
        d._check_re_enable(agent)

        assert d.db.count_total_runs("test/a") == 5

    def test_no_re_enable_without_max_runs(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        d.db.set_disabled_at("test/a", datetime.now(timezone.utc))

        agent = {"id": "test/a", "active": True, "max_runs": None}
        d._check_re_enable(agent)

        assert d.db.get_disabled_at("test/a") is not None

    def test_no_re_enable_when_inactive(self, tmp_path):
        d = self._make_dispatcher(tmp_path)
        for _ in range(5):
            d.db.mark_run("test/a", status="success")
        d.db.set_disabled_at("test/a", datetime.now(timezone.utc))

        agent = {"id": "test/a", "active": False, "max_runs": 5}
        d._check_re_enable(agent)

        assert d.db.count_total_runs("test/a") == 5
