"""Tests for the database layer."""

import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from zo_dispatcher.db import DispatcherDB


@pytest.fixture
def db():
    """Create a temp database and clean up after."""
    tmp = tempfile.mktemp(suffix=".db")
    database = DispatcherDB(tmp)
    yield database
    database.conn.close()
    os.unlink(tmp)


# --- Table creation ---

class TestTableCreation:
    def test_tables_exist(self, db):
        tables = [
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "agent_runs" in tables
        assert "webhooks" in tables
        assert "webhook_events" in tables
        assert "pending_notifications" in tables


# --- Agent runs ---

class TestAgentRuns:
    def test_begin_and_finish_run_updates_same_row(self, db):
        started_at = datetime.now(timezone.utc)
        run_id = db.begin_run(
            "test/agent",
            event_type="webhook.event",
            source="stripe",
            dispatched_at=started_at,
        )

        row = db.conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None
        assert row["agent_id"] == "test/agent"
        assert row["status"] == "started"
        assert row["event_type"] == "webhook.event"
        assert row["source"] == "stripe"

        db.finish_run(run_id, status="success", conv_id="conv-1", duration=12.5)
        updated = db.conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        assert updated["status"] == "success"
        assert updated["conv_id"] == "conv-1"
        assert updated["duration_seconds"] == 12.5
        assert db.conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1

    def test_mark_and_get_last_run(self, db):
        now = datetime.now(timezone.utc)
        db.mark_run("test/agent", dispatched_at=now, conv_id="conv_1")

        last = db.get_last_run("test/agent")
        assert last is not None
        assert isinstance(last, datetime)

    def test_get_last_run_returns_most_recent(self, db):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        new = datetime.now(timezone.utc) - timedelta(hours=1)
        db.mark_run("test/agent", dispatched_at=old)
        db.mark_run("test/agent", dispatched_at=new)

        last = db.get_last_run("test/agent")
        assert abs((last - new).total_seconds()) < 1

    def test_get_last_run_no_data(self, db):
        assert db.get_last_run("nonexistent/agent") is None

    def test_prune_old_runs(self, db):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        db.mark_run("test/old", dispatched_at=old, conv_id="old")
        db.mark_run("test/recent", dispatched_at=recent, conv_id="recent")

        count_before = db.conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        assert count_before == 2

        db.prune_old_runs(days=7)

        count_after = db.conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        assert count_after == 1

        remaining = db.conn.execute("SELECT agent_id FROM agent_runs").fetchone()
        assert remaining[0] == "test/recent"

    def test_count_runs_in_window(self, db):
        now = datetime.now(timezone.utc)
        db.mark_run("test/agent", dispatched_at=now - timedelta(seconds=100))
        db.mark_run("test/agent", dispatched_at=now - timedelta(seconds=50))
        db.mark_run("test/agent", dispatched_at=now - timedelta(seconds=10))
        db.mark_run("test/agent", dispatched_at=now - timedelta(seconds=7200))

        assert db.count_runs_in_window("test/agent", 3600) == 3
        assert db.count_runs_in_window("test/agent", 86400) == 4
        assert db.count_runs_in_window("other/agent", 3600) == 0


# --- Webhook sources ---

class TestWebhookSources:
    def test_add_and_get(self, db):
        db.conn.execute(
            "INSERT INTO webhooks (source, secret_env, signature_header, signature_algo, "
            "signature_prefix, event_type_path, event_id_path, transform_script, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stripe", "STRIPE_SECRET", "Stripe-Signature", "hmac-sha256-hex",
             "", "type", "id", None,
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()),
        )
        db.conn.commit()

        src = db.get_webhook_source("stripe")
        assert src is not None
        assert src["secret_env"] == "STRIPE_SECRET"
        assert src["signature_header"] == "Stripe-Signature"
        assert src["event_type_path"] == "type"

    def test_list_sources(self, db):
        now = datetime.now(timezone.utc).isoformat()
        for name in ("alpha", "beta"):
            db.conn.execute(
                "INSERT INTO webhooks (source, secret_env, signature_header, signature_algo, "
                "signature_prefix, event_type_path, event_id_path, transform_script, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, None, None, None, "", None, None, None, now, now),
            )
        db.conn.commit()

        sources = db.list_webhook_sources()
        assert len(sources) == 2
        names = [s["source"] for s in sources]
        assert "alpha" in names
        assert "beta" in names

    def test_get_nonexistent(self, db):
        assert db.get_webhook_source("nope") is None


# --- Deduplication ---

class TestDeduplication:
    def test_first_check_not_duplicate(self, db):
        assert db.check_dedupe("evt_1", "stripe") is False

    def test_record_then_check(self, db):
        db.record_event("evt_1", "stripe")
        assert db.check_dedupe("evt_1", "stripe") is True

    def test_different_id_not_duplicate(self, db):
        db.record_event("evt_1", "stripe")
        assert db.check_dedupe("evt_2", "stripe") is False

    def test_insert_or_ignore(self, db):
        db.record_event("evt_1", "stripe")
        db.record_event("evt_1", "stripe")  # should not raise
        count = db.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]
        assert count == 1

    def test_prune_old_events(self, db):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        recent_time = datetime.now(timezone.utc).isoformat()

        db.conn.execute(
            "INSERT INTO webhook_events (id, source, received_at) VALUES (?, ?, ?)",
            ("old_evt", "stripe", old_time),
        )
        db.conn.execute(
            "INSERT INTO webhook_events (id, source, received_at) VALUES (?, ?, ?)",
            ("new_evt", "stripe", recent_time),
        )
        db.conn.commit()

        db.prune_old_events(hours=24)
        count = db.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]
        assert count == 1
        remaining = db.conn.execute("SELECT id FROM webhook_events").fetchone()[0]
        assert remaining == "new_evt"


# --- Notification queue ---

class TestNotificationQueue:
    def test_queue_and_pop(self, db):
        db.queue_notification("discord/general", "Title", "Content", "con_1", "dispatcher-test-agent-7")
        db.queue_notification("sms", "Title 2", "Content 2")

        pending = db.pop_pending_notifications()
        assert len(pending) == 2
        assert pending[0]["channel_spec"] == "discord/general"
        assert pending[0]["title"] == "Title"
        assert pending[0]["memory_session_title"] == "dispatcher-test-agent-7"
        assert pending[1]["channel_spec"] == "sms"
        assert pending[1]["memory_session_title"] == ""

        remaining = db.pop_pending_notifications()
        assert len(remaining) == 0

    def test_pop_empty(self, db):
        assert db.pop_pending_notifications() == []
