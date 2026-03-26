import logging
import sqlite3
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("zo-dispatcher")


class DispatcherDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                dispatched_at TEXT NOT NULL,
                status TEXT,
                conv_id TEXT,
                duration_seconds REAL,
                event_type TEXT,
                source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_time
                ON agent_runs(agent_id, dispatched_at);

            CREATE TABLE IF NOT EXISTS webhooks (
                source TEXT PRIMARY KEY,
                secret_env TEXT,
                signature_header TEXT,
                signature_algo TEXT,
                signature_prefix TEXT DEFAULT '',
                event_type_path TEXT,
                event_id_path TEXT,
                transform_script TEXT,
                allow_unsigned BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhook_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                FOREIGN KEY (source) REFERENCES webhooks(source)
            );

            CREATE TABLE IF NOT EXISTS pending_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_spec TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                conv_id TEXT DEFAULT '',
                honcho_session_key TEXT DEFAULT '',
                queued_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_state (
                agent_id TEXT PRIMARY KEY,
                disabled_at TEXT
            );
        """)
        self.conn.commit()

    def _migrate(self):
        cursor = self.conn.execute("PRAGMA table_info(webhooks)")
        columns = {row[1] for row in cursor.fetchall()}
        if "allow_unsigned" not in columns:
            self.conn.execute("ALTER TABLE webhooks ADD COLUMN allow_unsigned BOOLEAN DEFAULT 0")
            self.conn.commit()
        if "disabled" not in columns:
            self.conn.execute("ALTER TABLE webhooks ADD COLUMN disabled BOOLEAN DEFAULT 0")
            self.conn.commit()

        pending_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(pending_notifications)").fetchall()}
        if "honcho_session_key" not in pending_columns:
            self.conn.execute("ALTER TABLE pending_notifications ADD COLUMN honcho_session_key TEXT DEFAULT ''")
            self.conn.commit()

    def get_last_run(self, agent_id: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT dispatched_at FROM agent_runs WHERE agent_id = ? ORDER BY dispatched_at DESC LIMIT 1",
            (agent_id,)
        ).fetchone()
        if row:
            return datetime.fromisoformat(row[0])
        return None

    def begin_run(
        self,
        agent_id: str,
        status: str = "started",
        event_type: str = "",
        source: str = "",
        dispatched_at: datetime | None = None,
    ) -> int:
        ts = (dispatched_at or datetime.now(timezone.utc)).isoformat()
        cursor = self.conn.execute(
            "INSERT INTO agent_runs (agent_id, dispatched_at, status, conv_id, duration_seconds, event_type, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, ts, status, "", 0, event_type, source)
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, conv_id: str = "", duration: float = 0):
        self.conn.execute(
            "UPDATE agent_runs SET status = ?, conv_id = ?, duration_seconds = ? WHERE id = ?",
            (status, conv_id, duration, run_id)
        )
        self.conn.commit()

    def mark_run(self, agent_id: str, status: str = "", conv_id: str = "",
                 duration: float = 0, event_type: str = "", source: str = "",
                 dispatched_at: datetime | None = None):
        ts = (dispatched_at or datetime.now(timezone.utc)).isoformat()
        self.conn.execute(
            "INSERT INTO agent_runs (agent_id, dispatched_at, status, conv_id, duration_seconds, event_type, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, ts, status, conv_id, duration, event_type, source)
        )
        self.conn.commit()

    def prune_old_runs(self, days: int = 7):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.conn.execute("DELETE FROM agent_runs WHERE dispatched_at < ?", (cutoff,))
        self.conn.commit()

    def get_webhook_source(self, source: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM webhooks WHERE source = ?", (source,)).fetchone()
        return dict(row) if row else None

    def list_webhook_sources(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM webhooks ORDER BY source").fetchall()
        return [dict(row) for row in rows]

    def check_dedupe(self, event_id: str, source: str) -> bool:
        row = self.conn.execute("SELECT id FROM webhook_events WHERE id = ?", (event_id,)).fetchone()
        return row is not None

    def record_event(self, event_id: str, source: str):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO webhook_events (id, source, received_at) VALUES (?, ?, ?)",
            (event_id, source, now)
        )
        self.conn.commit()

    def prune_old_events(self, hours: int = 24):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self.conn.execute("DELETE FROM webhook_events WHERE received_at < ?", (cutoff,))
        self.conn.commit()

    def count_total_runs(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        return row[0]

    def set_disabled_at(self, agent_id: str, timestamp: datetime):
        ts = timestamp.isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO agent_state (agent_id, disabled_at) VALUES (?, ?)",
            (agent_id, ts)
        )
        self.conn.commit()

    def get_disabled_at(self, agent_id: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT disabled_at FROM agent_state WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    def clear_disabled(self, agent_id: str):
        self.conn.execute("DELETE FROM agent_state WHERE agent_id = ?", (agent_id,))
        self.conn.commit()

    def clear_runs(self, agent_id: str):
        self.conn.execute("DELETE FROM agent_runs WHERE agent_id = ?", (agent_id,))
        self.conn.commit()

    def count_runs_in_window(self, agent_id: str, window_seconds: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE agent_id = ? AND dispatched_at > ?",
            (agent_id, cutoff)
        ).fetchone()
        return row[0]

    def set_webhook_disabled(self, source: str, disabled: bool) -> bool:
        row = self.conn.execute("SELECT source FROM webhooks WHERE source = ?", (source,)).fetchone()
        if not row:
            return False
        self.conn.execute("UPDATE webhooks SET disabled = ?, updated_at = ? WHERE source = ?",
                          (1 if disabled else 0, datetime.now(timezone.utc).isoformat(), source))
        self.conn.commit()
        return True

    def is_webhook_disabled(self, source: str) -> bool:
        row = self.conn.execute("SELECT disabled FROM webhooks WHERE source = ?", (source,)).fetchone()
        return bool(row and row[0])

    def count_runs_for_source(self, source: str, window_seconds: int) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM agent_runs WHERE source = ? AND dispatched_at > ? GROUP BY status",
            (source, cutoff)
        ).fetchall()
        counts = {}
        for row in rows:
            counts[row[0]] = row[1]
        return counts

    def count_deduped_for_source(self, source: str, window_seconds: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM webhook_events WHERE source = ? AND received_at > ?",
            (source, cutoff)
        ).fetchone()
        return row[0]

    def queue_notification(
        self,
        channel_spec: str,
        title: str,
        content: str,
        conv_id: str = "",
        honcho_session_key: str = "",
    ):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO pending_notifications (channel_spec, title, content, conv_id, honcho_session_key, queued_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_spec, title, content, conv_id, honcho_session_key, now)
        )
        self.conn.commit()

    def pop_pending_notifications(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM pending_notifications ORDER BY id").fetchall()
        if not rows:
            return []
        self.conn.execute("DELETE FROM pending_notifications")
        self.conn.commit()
        return [dict(row) for row in rows]
