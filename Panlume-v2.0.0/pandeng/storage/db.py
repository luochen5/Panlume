"""SQLite 连接、初始化与第一阶段只读查询。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(schema)
            self._upgrade_position_lots(connection)
            connection.execute(
                """
                INSERT INTO app_metadata(key, value, updated_at)
                VALUES ('schema_version', '6', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO app_metadata(key, value, updated_at)
                VALUES ('initialized_at', ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (now, now),
            )
            connection.execute(
                "INSERT INTO run_logs(level, event, message, created_at) "
                "VALUES ('INFO', 'database_initialized', ?, ?)",
                (str(self.path), now),
            )

    @staticmethod
    def _upgrade_position_lots(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(position_lots)")
        }
        additions = {
            "active": "INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))",
            "deactivated_at": "TEXT",
            "deactivation_reason": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE position_lots ADD COLUMN {name} {definition}")

    def is_initialized(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'app_metadata'"
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row["value"])

    def list_position_lots(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, stock_code, quantity, buy_date,
                           stop_loss_price, confirmed_at, active,
                           deactivated_at, deactivation_reason
                    FROM position_lots
                    ORDER BY stock_code, buy_date, id
                    """
                )
            )
