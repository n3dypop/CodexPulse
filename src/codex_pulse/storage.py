from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .models import ResumeTarget, TurnUsage, UsageSample


_USAGE_SAMPLE_COLUMNS = (
    "observed_at",
    "primary_used_percent",
    "primary_window_minutes",
    "primary_resets_at",
    "secondary_used_percent",
    "secondary_window_minutes",
    "secondary_resets_at",
    "credits_balance",
    "credits_has_credits",
    "credits_unlimited",
    "ordinary_usage_allowed",
    "plan_type",
    "limit_id",
    "limit_reached_type",
    "model_slug",
    "primary_limit_available",
)

_INSERT_USAGE_SAMPLE = """
    INSERT INTO usage_samples (
        observed_at,
        primary_used_percent,
        primary_window_minutes,
        primary_resets_at,
        secondary_used_percent,
        secondary_window_minutes,
        secondary_resets_at,
        credits_balance,
        credits_has_credits,
        credits_unlimited,
        ordinary_usage_allowed,
        plan_type,
        limit_id,
        limit_reached_type,
        model_slug,
        primary_limit_available
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_TURN_USAGE_COLUMNS = (
    "turn_id",
    "thread_id",
    "root_turn_id",
    "session_file",
    "started_at",
    "completed_at",
    "duration_ms",
    "model",
    "effort",
    "cwd",
    "project_name",
    "is_subagent",
    "status",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "max_request_input_tokens",
    "context_window",
    "updated_at",
)

_UPSERT_TURN_USAGE = """
    INSERT INTO turn_usage (
        turn_id,
        thread_id,
        root_turn_id,
        session_file,
        started_at,
        completed_at,
        duration_ms,
        model,
        effort,
        cwd,
        project_name,
        is_subagent,
        status,
        input_tokens,
        cached_input_tokens,
        cache_write_input_tokens,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
        max_request_input_tokens,
        context_window,
        updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(turn_id) DO UPDATE SET
        thread_id = excluded.thread_id,
        root_turn_id = excluded.root_turn_id,
        session_file = excluded.session_file,
        started_at = excluded.started_at,
        completed_at = excluded.completed_at,
        duration_ms = excluded.duration_ms,
        model = excluded.model,
        effort = excluded.effort,
        cwd = excluded.cwd,
        project_name = excluded.project_name,
        is_subagent = excluded.is_subagent,
        status = excluded.status,
        input_tokens = excluded.input_tokens,
        cached_input_tokens = excluded.cached_input_tokens,
        cache_write_input_tokens = excluded.cache_write_input_tokens,
        output_tokens = excluded.output_tokens,
        reasoning_output_tokens = excluded.reasoning_output_tokens,
        total_tokens = excluded.total_tokens,
        max_request_input_tokens = excluded.max_request_input_tokens,
        context_window = excluded.context_window,
        updated_at = excluded.updated_at
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_samples (
                    id INTEGER PRIMARY KEY,
                    observed_at INTEGER NOT NULL,
                    primary_used_percent REAL NOT NULL,
                    primary_window_minutes INTEGER NOT NULL,
                    primary_resets_at INTEGER NOT NULL,
                    secondary_used_percent REAL,
                    secondary_window_minutes INTEGER,
                    secondary_resets_at INTEGER,
                    credits_balance REAL,
                    credits_has_credits INTEGER NOT NULL,
                    credits_unlimited INTEGER NOT NULL,
                    ordinary_usage_allowed INTEGER,
                    plan_type TEXT,
                    limit_id TEXT NOT NULL,
                    limit_reached_type TEXT,
                    model_slug TEXT,
                    primary_limit_available INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_samples_observed
                    ON usage_samples(observed_at);
                CREATE INDEX IF NOT EXISTS idx_samples_reset
                    ON usage_samples(primary_resets_at, observed_at);
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS turn_usage (
                    turn_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    root_turn_id TEXT,
                    session_file TEXT NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER,
                    duration_ms INTEGER,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    cwd TEXT,
                    project_name TEXT NOT NULL,
                    is_subagent INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    max_request_input_tokens INTEGER NOT NULL DEFAULT 0,
                    context_window INTEGER,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turn_usage_time
                    ON turn_usage(started_at, completed_at);
                CREATE INDEX IF NOT EXISTS idx_turn_usage_profile
                    ON turn_usage(model, effort, completed_at);
                CREATE INDEX IF NOT EXISTS idx_turn_usage_project
                    ON turn_usage(project_name, completed_at);
                CREATE TABLE IF NOT EXISTS session_offsets (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    offset INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_attributions (
                    interval_end INTEGER NOT NULL,
                    turn_id TEXT NOT NULL,
                    used_percent REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    PRIMARY KEY(interval_end, turn_id),
                    FOREIGN KEY(turn_id) REFERENCES turn_usage(turn_id)
                );
                CREATE INDEX IF NOT EXISTS idx_attributions_turn
                    ON usage_attributions(turn_id);
                CREATE TABLE IF NOT EXISTS forecast_predictions (
                    id INTEGER PRIMARY KEY,
                    predicted_at INTEGER NOT NULL,
                    reset_at INTEGER NOT NULL,
                    estimated_exhaustion_at INTEGER NOT NULL,
                    observed_remaining REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    actual_exhaustion_at INTEGER,
                    outcome TEXT,
                    UNIQUE(predicted_at, reset_at)
                );
                CREATE INDEX IF NOT EXISTS idx_predictions_reset
                    ON forecast_predictions(reset_at, predicted_at);
                CREATE TABLE IF NOT EXISTS resume_queue (
                    thread_id TEXT PRIMARY KEY,
                    thread_name TEXT,
                    project_id TEXT,
                    project_name TEXT,
                    stopped_at INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    queued_at INTEGER NOT NULL,
                    resumed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_resume_queue_state
                    ON resume_queue(state, stopped_at DESC);
                CREATE TABLE IF NOT EXISTS discord_cleanup (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    delete_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_discord_cleanup_due
                    ON discord_cleanup(delete_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(usage_samples)")
            }
            if "primary_limit_available" not in columns:
                connection.execute(
                    "ALTER TABLE usage_samples ADD COLUMN "
                    "primary_limit_available INTEGER NOT NULL DEFAULT 1"
                )

    def add_sample(self, sample: UsageSample) -> None:
        values = sample.as_dict()
        with self._connection() as connection:
            connection.execute(
                _INSERT_USAGE_SAMPLE,
                [values[name] for name in _USAGE_SAMPLE_COLUMNS],
            )

    @staticmethod
    def _sample(row: sqlite3.Row) -> UsageSample:
        value = dict(row)
        value.pop("id", None)
        for name in (
            "credits_has_credits",
            "credits_unlimited",
            "primary_limit_available",
        ):
            value[name] = bool(value[name])
        if value["ordinary_usage_allowed"] is not None:
            value["ordinary_usage_allowed"] = bool(value["ordinary_usage_allowed"])
        return UsageSample(**value)

    def latest_sample(self) -> Optional[UsageSample]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM usage_samples ORDER BY observed_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._sample(row) if row is not None else None

    def recent_samples(
        self, since: int, reset_at: Optional[int] = None
    ) -> List[UsageSample]:
        query = "SELECT * FROM usage_samples WHERE observed_at >= ?"
        params: list[Any] = [int(since)]
        if reset_at is not None:
            query += " AND primary_resets_at = ?"
            params.append(int(reset_at))
        query += " ORDER BY observed_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._sample(row) for row in rows]

    def prune(self, before: int) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM usage_samples WHERE observed_at < ?", (int(before),)
            )
            return cursor.rowcount

    def maintain(self, history_before: int, resumed_before: int) -> dict[str, Any]:
        """Prune bounded history and compact SQLite when reclaimed space is material."""
        history_cutoff = int(history_before)
        resumed_cutoff = int(resumed_before)
        statements = (
            (
                "usage_attributions",
                "DELETE FROM usage_attributions WHERE interval_end < ?",
                (history_cutoff,),
            ),
            (
                "usage_samples",
                "DELETE FROM usage_samples WHERE observed_at < ?",
                (history_cutoff,),
            ),
            (
                "events",
                "DELETE FROM events WHERE created_at < ?",
                (history_cutoff,),
            ),
            (
                "forecast_predictions",
                "DELETE FROM forecast_predictions WHERE predicted_at < ?",
                (history_cutoff,),
            ),
            (
                "session_offsets",
                "DELETE FROM session_offsets WHERE updated_at < ?",
                (history_cutoff,),
            ),
            (
                "session_metadata",
                "DELETE FROM state WHERE key GLOB 'session_meta:*' AND updated_at < ?",
                (history_cutoff,),
            ),
            (
                "resume_queue",
                """
                DELETE FROM resume_queue
                WHERE (state = 'resumed' AND COALESCE(resumed_at, queued_at) < ?)
                   OR (state = 'pending' AND stopped_at < ?)
                """,
                (resumed_cutoff, history_cutoff),
            ),
            (
                "turn_usage",
                """
                DELETE FROM turn_usage
                WHERE COALESCE(completed_at, started_at, updated_at) < ?
                  AND turn_id NOT IN (
                      SELECT DISTINCT turn_id FROM usage_attributions
                  )
                """,
                (history_cutoff,),
            ),
        )
        deleted: dict[str, int] = {}
        with self._connection() as connection:
            for name, statement, parameters in statements:
                cursor = connection.execute(statement, parameters)
                deleted[name] = max(0, int(cursor.rowcount))

        self.checkpoint()
        compacted = self.compact_if_needed()
        if compacted:
            self.checkpoint()
        return {
            "deleted": deleted,
            "compacted": compacted,
            "database_bytes": self.database_size_bytes(),
        }

    def checkpoint(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()

    def compact_if_needed(
        self, *, minimum_free_pages: int = 256, minimum_free_ratio: float = 0.25
    ) -> bool:
        connection = self._connect()
        try:
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            should_compact = (
                free_pages >= minimum_free_pages
                and page_count > 0
                and free_pages / page_count >= minimum_free_ratio
            )
            if should_compact:
                connection.execute("VACUUM")
            return should_compact
        finally:
            connection.close()

    def database_size_bytes(self) -> int:
        paths = (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        )
        total = 0
        for path in paths:
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row is not None else default

    def set_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, int(time.time())),
            )

    def schedule_discord_cleanup(
        self,
        message_id: int,
        channel_id: int,
        delete_at: int,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO discord_cleanup(
                    message_id, channel_id, delete_at, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    delete_at = excluded.delete_at
                """,
                (
                    int(message_id),
                    int(channel_id),
                    int(delete_at),
                    int(time.time()),
                ),
            )

    def due_discord_cleanups(
        self,
        now: Optional[int] = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, int]]:
        due_at = int(time.time()) if now is None else int(now)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT message_id, channel_id, delete_at
                FROM discord_cleanup
                WHERE delete_at <= ?
                ORDER BY delete_at, message_id
                LIMIT ?
                """,
                (due_at, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "message_id": int(row["message_id"]),
                "channel_id": int(row["channel_id"]),
                "delete_at": int(row["delete_at"]),
            }
            for row in rows
        ]

    def complete_discord_cleanup(self, message_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM discord_cleanup WHERE message_id = ?",
                (int(message_id),),
            )

    def has_event(self, event_key: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM events WHERE event_key = ?", (event_key,)
            ).fetchone()
        return row is not None

    def mark_event(self, event_key: str, payload: Any = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO events(event_key, created_at, payload) VALUES (?, ?, ?)",
                (event_key, int(time.time()), encoded),
            )

    def upsert_turn(self, turn: TurnUsage) -> None:
        values = turn.as_dict()
        with self._connection() as connection:
            connection.execute(
                _UPSERT_TURN_USAGE,
                [values[name] for name in _TURN_USAGE_COLUMNS],
            )

    @staticmethod
    def _turn(row: sqlite3.Row) -> TurnUsage:
        value = dict(row)
        value["is_subagent"] = bool(value["is_subagent"])
        return TurnUsage(**value)

    def recent_turns(
        self,
        since: int,
        *,
        completed_only: bool = False,
        limit: Optional[int] = None,
    ) -> List[TurnUsage]:
        query = "SELECT * FROM turn_usage WHERE COALESCE(completed_at, started_at, updated_at) >= ?"
        params: list[Any] = [int(since)]
        if completed_only:
            query += " AND completed_at IS NOT NULL"
        query += " ORDER BY COALESCE(completed_at, started_at, updated_at) DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._turn(row) for row in rows]

    def get_turn(self, turn_id: str) -> Optional[TurnUsage]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM turn_usage WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return self._turn(row) if row is not None else None

    def turns_overlapping(self, started_at: int, ended_at: int) -> List[TurnUsage]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM turn_usage
                WHERE started_at IS NOT NULL
                  AND started_at <= ?
                  AND COALESCE(completed_at, updated_at) >= ?
                  AND total_tokens > 0
                ORDER BY started_at
                """,
                (int(ended_at), int(started_at)),
            ).fetchall()
        return [self._turn(row) for row in rows]

    def get_session_offset(self, path: str, inode: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT inode, offset FROM session_offsets WHERE path = ?", (path,)
            ).fetchone()
        if row is None or int(row["inode"]) != int(inode):
            return 0
        return int(row["offset"])

    def set_session_offset(self, path: str, inode: int, offset: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO session_offsets(path, inode, offset, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    inode = excluded.inode,
                    offset = excluded.offset,
                    updated_at = excluded.updated_at
                """,
                (path, int(inode), int(offset), int(time.time())),
            )

    def replace_attributions(
        self,
        interval_end: int,
        values: list[tuple[str, float, str]],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM usage_attributions WHERE interval_end = ?",
                (int(interval_end),),
            )
            connection.executemany(
                """
                INSERT INTO usage_attributions(
                    interval_end, turn_id, used_percent, confidence
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (int(interval_end), turn_id, float(used), confidence)
                    for turn_id, used, confidence in values
                ],
            )

    def analytics_rows(self, since: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT t.*,
                       COALESCE(SUM(a.used_percent), 0.0) AS attributed_percent,
                       CASE
                           WHEN SUM(CASE a.confidence WHEN 'low' THEN 1 ELSE 0 END) > 0
                               THEN 'low'
                           WHEN SUM(CASE a.confidence WHEN 'medium' THEN 1 ELSE 0 END) > 0
                               THEN 'medium'
                           WHEN COUNT(a.turn_id) > 0 THEN 'high'
                           ELSE 'unavailable'
                       END AS attribution_confidence
                FROM turn_usage t
                LEFT JOIN usage_attributions a ON a.turn_id = t.turn_id
                WHERE COALESCE(t.completed_at, t.started_at, t.updated_at) >= ?
                GROUP BY t.turn_id
                ORDER BY COALESCE(t.completed_at, t.started_at, t.updated_at) DESC
                """,
                (int(since),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_prediction(
        self,
        predicted_at: int,
        reset_at: int,
        estimated_exhaustion_at: int,
        observed_remaining: float,
        confidence: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO forecast_predictions(
                    predicted_at, reset_at, estimated_exhaustion_at,
                    observed_remaining, confidence
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(predicted_at),
                    int(reset_at),
                    int(estimated_exhaustion_at),
                    float(observed_remaining),
                    confidence,
                ),
            )

    def resolve_predictions(
        self,
        reset_at: int,
        *,
        actual_exhaustion_at: Optional[int],
        outcome: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE forecast_predictions
                SET actual_exhaustion_at = ?, outcome = ?
                WHERE reset_at = ? AND outcome IS NULL
                """,
                (actual_exhaustion_at, outcome, int(reset_at)),
            )

    def prediction_rows(self, since: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM forecast_predictions
                WHERE predicted_at >= ? AND outcome IS NOT NULL
                ORDER BY predicted_at DESC
                """,
                (int(since),),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue_resume_targets(self, targets: list[ResumeTarget]) -> None:
        now = int(time.time())
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO resume_queue(
                    thread_id, thread_name, project_id, project_name,
                    stopped_at, state, queued_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    thread_name = excluded.thread_name,
                    project_id = excluded.project_id,
                    project_name = excluded.project_name,
                    stopped_at = excluded.stopped_at,
                    state = CASE
                        WHEN resume_queue.state = 'resumed' THEN resume_queue.state
                        ELSE 'pending'
                    END
                """,
                [
                    (
                        target.thread_id,
                        target.thread_name,
                        target.project_id,
                        target.project_name,
                        target.stopped_at,
                        now,
                    )
                    for target in targets
                ],
            )

    def pending_resume_targets(self, limit: int = 25) -> list[ResumeTarget]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT thread_id, thread_name, project_id, project_name, stopped_at
                FROM resume_queue
                WHERE state = 'pending'
                ORDER BY stopped_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [ResumeTarget(**dict(row)) for row in rows]

    def mark_resume_target_resumed(self, thread_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE resume_queue
                SET state = 'resumed', resumed_at = ?
                WHERE thread_id = ?
                """,
                (int(time.time()), thread_id),
            )
