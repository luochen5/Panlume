"""桌面版数据库状态、SQLite 在线备份和保守清理。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pandeng.data.schedule import CHINA_TZ
from pandeng.storage.db import Database


TABLES = (
    "minute_bars", "daily_qfq_bars", "trading_calendar", "indicator_snapshots",
    "rule_events", "summary_tasks", "review_runs", "run_logs", "position_lots",
)


class DataManagementError(RuntimeError):
    pass


@dataclass(frozen=True)
class CleanupPreview:
    minute_ids: tuple[int, ...]
    log_ids: tuple[int, ...]
    protected_minutes: int
    minute_cutoff: str
    log_cutoff: str
    token: str

    @property
    def total(self) -> int:
        return len(self.minute_ids) + len(self.log_ids)


class DesktopDataManager:
    def __init__(
        self,
        database_path: Path,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = Database(database_path)
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    def initialize(self) -> None:
        self.database.initialize()

    def schema_version(self) -> str:
        if not self.database.path.exists():
            return "未创建"
        return self.database.metadata("schema_version") or "未知"

    def statistics(self) -> dict[str, int]:
        if not self.database.path.exists():
            return {table: 0 for table in TABLES}
        with self.database.connect() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in TABLES
            }

    def database_size(self) -> int:
        return sum(
            path.stat().st_size
            for path in (
                self.database.path,
                Path(str(self.database.path) + "-wal"),
                Path(str(self.database.path) + "-shm"),
            )
            if path.exists()
        )

    def backup(self, target: Path) -> Path:
        self.initialize()
        target = target.expanduser().resolve()
        if target == self.database.path.resolve():
            raise DataManagementError("备份不能覆盖正在使用的数据库。")
        if target.exists():
            raise DataManagementError("备份文件已存在，请换一个文件名；程序不会覆盖旧备份。")
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            with self.database.connect() as source:
                source.backup(destination)
            row = destination.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise DataManagementError("备份完整性检查未通过。")
        except Exception:
            destination.close()
            if target.exists():
                target.unlink()
            raise
        else:
            destination.close()
        return target

    def preview_cleanup(self, minute_days: int, log_days: int) -> CleanupPreview:
        if not 5 <= minute_days <= 365 or not 7 <= log_days <= 365:
            raise DataManagementError("清理保留天数超出允许范围。")
        self.initialize()
        now = self._now()
        minute_cutoff = (now.astimezone(CHINA_TZ) - timedelta(days=minute_days)).isoformat()
        log_cutoff = (now.astimezone(timezone.utc) - timedelta(days=log_days)).isoformat()
        with self.database.connect() as connection:
            protected_ids: set[int] = set()
            review_days: set[tuple[str, str]] = set()
            for row in connection.execute(
                "SELECT latest_minute_bar_id, source_bar_ids_json FROM indicator_snapshots"
            ):
                if row["latest_minute_bar_id"] is not None:
                    protected_ids.add(int(row["latest_minute_bar_id"]))
                try:
                    protected_ids.update(int(item) for item in json.loads(row["source_bar_ids_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise DataManagementError("指标来源记录异常，已停止清理预览。") from exc
            for row in connection.execute("SELECT session_date, stock_code FROM review_runs"):
                review_days.add((str(row["session_date"]), str(row["stock_code"])))

            old_minutes = list(connection.execute(
                "SELECT id, stock_code, bar_time FROM minute_bars WHERE bar_time < ? ORDER BY id",
                (minute_cutoff,),
            ))
            minute_ids = tuple(
                int(row["id"]) for row in old_minutes
                if int(row["id"]) not in protected_ids
                and (str(row["bar_time"])[:10], str(row["stock_code"])) not in review_days
            )
            log_ids = tuple(
                int(row["id"]) for row in connection.execute(
                    "SELECT id FROM run_logs WHERE created_at < ? ORDER BY id", (log_cutoff,)
                )
            )
        protected_minutes = len(old_minutes) - len(minute_ids)
        token_payload = json.dumps(
            [minute_ids, log_ids, minute_cutoff, log_cutoff], separators=(",", ":")
        ).encode("ascii")
        return CleanupPreview(
            minute_ids=minute_ids,
            log_ids=log_ids,
            protected_minutes=protected_minutes,
            minute_cutoff=minute_cutoff,
            log_cutoff=log_cutoff,
            token=hashlib.sha256(token_payload).hexdigest(),
        )

    def cleanup(self, preview: CleanupPreview, minute_days: int, log_days: int) -> tuple[int, int]:
        current = self.preview_cleanup(minute_days, log_days)
        if current.token != preview.token:
            raise DataManagementError("数据或保留设置已变化，请重新预览后再清理。")
        if not preview.minute_ids and not preview.log_ids:
            return (0, 0)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_ids(connection, "minute_bars", preview.minute_ids)
            self._delete_ids(connection, "run_logs", preview.log_ids)
            connection.execute(
                "INSERT INTO run_logs(level, event, message, created_at) "
                "VALUES ('INFO', 'desktop_cleanup', ?, ?)",
                (
                    json.dumps({
                        "deleted_minutes": len(preview.minute_ids),
                        "deleted_logs": len(preview.log_ids),
                        "protected_minutes": preview.protected_minutes,
                        "minute_cutoff": preview.minute_cutoff,
                        "log_cutoff": preview.log_cutoff,
                    }, ensure_ascii=False),
                    self._now().astimezone(timezone.utc).isoformat(),
                ),
            )
        return (len(preview.minute_ids), len(preview.log_ids))

    @staticmethod
    def _delete_ids(connection: sqlite3.Connection, table: str, ids: tuple[int, ...]) -> None:
        if table not in {"minute_bars", "run_logs"}:
            raise DataManagementError("清理目标表不在允许范围内。")
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", chunk)
