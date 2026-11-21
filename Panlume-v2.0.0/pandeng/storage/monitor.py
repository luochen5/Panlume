"""第二阶段 B 的调度状态与可追溯事件读写。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_monitor_state(database: Database, session_date: str) -> sqlite3.Row:
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM monitor_state WHERE id = 1").fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO monitor_state(
                    id, session_date, status, calendar_status, daily_status, updated_at
                ) VALUES (1, ?, 'idle', 'unknown', 'not_ready', ?)
                """,
                (session_date, _now()),
            )
        elif row["session_date"] != session_date:
            connection.execute(
                """
                UPDATE monitor_state
                SET session_date = ?, status = 'idle', calendar_status = 'unknown',
                    daily_status = 'not_ready', last_round_at = NULL,
                    last_successful_round_at = NULL, next_poll_at = NULL,
                    stop_reason = NULL, updated_at = ?
                WHERE id = 1
                """,
                (session_date, _now()),
            )
        return connection.execute("SELECT * FROM monitor_state WHERE id = 1").fetchone()


def get_monitor_state(database: Database) -> sqlite3.Row:
    with database.connect() as connection:
        return connection.execute("SELECT * FROM monitor_state WHERE id = 1").fetchone()


def update_monitor_state(database: Database, **fields: Any) -> None:
    allowed = {
        "status", "calendar_status", "daily_status", "last_round_at",
        "last_successful_round_at", "next_poll_at", "stop_reason",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"不允许更新调度字段：{', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = [fields[key] for key in fields]
    with database.connect() as connection:
        connection.execute(
            f"UPDATE monitor_state SET {assignments}, updated_at = ? WHERE id = 1",
            (*values, _now()),
        )


def get_code_states(database: Database, session_date: str) -> dict[str, sqlite3.Row]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM monitor_code_states WHERE session_date = ?",
            (session_date,),
        ).fetchall()
    return {row["stock_code"]: row for row in rows}


def save_code_state(
    database: Database,
    session_date: str,
    stock_code: str,
    *,
    last_contiguous_time: str | None,
    last_attempt_time: str | None,
    status: str,
    unknown_reason: str | None,
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO monitor_code_states(
                session_date, stock_code, last_contiguous_time, last_attempt_time,
                status, unknown_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date, stock_code) DO UPDATE SET
                last_contiguous_time = excluded.last_contiguous_time,
                last_attempt_time = excluded.last_attempt_time,
                status = excluded.status,
                unknown_reason = excluded.unknown_reason,
                updated_at = excluded.updated_at
            """,
            (
                session_date, stock_code, last_contiguous_time, last_attempt_time,
                status, unknown_reason, _now(),
            ),
        )


def start_monitor_round(
    database: Database,
    session_date: str,
    *,
    trigger: str,
    recovery: bool,
    scheduled_at: datetime,
    target_time: datetime,
) -> int:
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO monitor_rounds(
                session_date, trigger, recovery, scheduled_at, target_time,
                started_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'unknown')
            """,
            (
                session_date, trigger, int(recovery), scheduled_at.isoformat(),
                target_time.isoformat(), _now(),
            ),
        )
        return int(cursor.lastrowid)


def finish_monitor_round(
    database: Database,
    round_id: int,
    *,
    status: str,
    analysis_ready_count: int = 0,
    received: int = 0,
    accepted: int = 0,
    duplicates: int = 0,
    rejected: int = 0,
    message: str = "",
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE monitor_rounds
            SET finished_at = ?, status = ?, analysis_ready_count = ?,
                received_count = ?, accepted_count = ?, duplicate_count = ?,
                rejected_count = ?, message = ?
            WHERE id = ?
            """,
            (
                _now(), status, analysis_ready_count, received, accepted,
                duplicates, rejected, message, round_id,
            ),
        )


def add_monitor_event(
    database: Database,
    session_date: str,
    event_type: str,
    message: str,
    *,
    round_id: int | None = None,
    stock_code: str | None = None,
    data_time: datetime | str | None = None,
) -> int:
    if isinstance(data_time, datetime):
        data_time = data_time.isoformat()
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO monitor_events(
                round_id, session_date, stock_code, event_type,
                data_time, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, session_date, stock_code, event_type, data_time, message, _now()),
        )
        return int(cursor.lastrowid)


def add_run_log(database: Database, event: str, message: str, level: str = "INFO") -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO run_logs(level, event, message, created_at) VALUES (?, ?, ?, ?)",
            (level, event, message, _now()),
        )


def recent_monitor_rounds(database: Database, limit: int = 10) -> list[sqlite3.Row]:
    with database.connect() as connection:
        return list(
            connection.execute(
                "SELECT * FROM monitor_rounds ORDER BY id DESC LIMIT ?", (limit,)
            )
        )


def recent_monitor_events(database: Database, limit: int = 20) -> list[sqlite3.Row]:
    with database.connect() as connection:
        return list(
            connection.execute(
                "SELECT * FROM monitor_events ORDER BY id DESC LIMIT ?", (limit,)
            )
        )


def session_round_count(database: Database, session_date: str) -> int:
    with database.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM monitor_rounds WHERE session_date = ?",
                (session_date,),
            ).fetchone()[0]
        )


def missing_minutes(
    database: Database,
    stock_code: str,
    expected: list[datetime],
) -> list[datetime]:
    if not expected:
        return []
    start, end = expected[0], expected[-1]
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT bar_time FROM minute_bars
            WHERE stock_code = ? AND bar_time >= ? AND bar_time <= ?
            """,
            (stock_code, start.isoformat(), end.isoformat()),
        ).fetchall()
    saved = {datetime.fromisoformat(row["bar_time"]) for row in rows}
    return [point for point in expected if point not in saved]


def current_daily_confirmation(
    database: Database, session_date: str, stock_codes: tuple[str, ...]
) -> bool:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT stock_code, confirmed FROM daily_qfq_bars WHERE trade_date = ?",
            (session_date,),
        ).fetchall()
    by_code = {row["stock_code"]: bool(row["confirmed"]) for row in rows}
    return all(by_code.get(code) is True for code in stock_codes)
