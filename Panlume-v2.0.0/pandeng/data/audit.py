"""只读行情体检：把日历、分钟完整性、价格线、日 K 和检查点分开报告。

本模块不补数、不修复冲突、不计算规则，也不调用模型。它只读取 SQLite，
帮助在接入真实数据源前确认“哪些数据可用、哪些结论必须保持未知”。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from pandeng.storage.db import Database

from .schedule import CHINA_TZ, is_session_minute, session_minutes


@dataclass(frozen=True)
class CodeAudit:
    stock_code: str
    status: str
    expected_minutes: int
    saved_minutes: int
    confirmed_minutes: int
    unconfirmed_minutes: int
    missing_times: tuple[str, ...]
    latest_minute_time: str | None
    target_minute_time: str | None
    target_confirmed: bool | None
    price_line_available: bool
    quality_error_count: int
    sources: tuple[str, ...]
    checkpoint_time: str | None
    checkpoint_status: str | None
    daily_status: str
    daily_source: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AuditReport:
    session_date: date
    as_of_time: datetime
    calendar_status: str
    calendar_source: str | None
    status: str
    codes: tuple[CodeAudit, ...]

    @property
    def complete_count(self) -> int:
        return sum(item.status == "ok" for item in self.codes)

    @property
    def price_only_count(self) -> int:
        return sum(item.status == "price_only" for item in self.codes)

    @property
    def unknown_count(self) -> int:
        return sum(item.status == "unknown" for item in self.codes)


def _validate_as_of(session_date: date, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as-of-time 必须带时区，例如 +08:00。")
    local = value.astimezone(CHINA_TZ)
    if local.date() != session_date:
        raise ValueError("as-of-time 的日期必须与 session-date 相同。")
    if not is_session_minute(local):
        raise ValueError("as-of-time 必须是 09:31～11:30 或 13:01～15:00 的整分钟。")
    return local


def _quality_counts(
    connection: sqlite3.Connection,
    code: str,
    start_key: str,
    target_key: str,
) -> tuple[int, int]:
    minute_total = int(connection.execute(
        """
        SELECT COUNT(*) FROM data_quality_issues
        WHERE stock_code = ? AND severity = 'error' AND data_kind = 'minute'
          AND data_time >= ? AND data_time <= ?
        """,
        (code, start_key, target_key),
    ).fetchone()[0])
    target = int(connection.execute(
        """
        SELECT COUNT(*) FROM data_quality_issues
        WHERE stock_code = ? AND severity = 'error' AND data_kind = 'minute'
          AND data_time = ?
        """,
        (code, target_key),
    ).fetchone()[0])
    return minute_total, target


def _daily_audit(
    connection: sqlite3.Connection, code: str, session_key: str
) -> tuple[str, str | None, str | None]:
    row = connection.execute(
        """
        SELECT confirmed, source FROM daily_qfq_bars
        WHERE stock_code = ? AND trade_date = ? AND adjustment = 'qfq'
        """,
        (code, session_key),
    ).fetchone()
    daily_errors = int(connection.execute(
        """
        SELECT COUNT(*) FROM data_quality_issues
        WHERE stock_code = ? AND data_kind = 'daily_qfq'
          AND data_time = ? AND severity = 'error'
        """,
        (code, session_key),
    ).fetchone()[0])
    if daily_errors:
        return "unknown", None if row is None else row["source"], "当日日线存在质量冲突"
    if row is None:
        return "missing", None, "当日明确前复权日 K 尚未保存"
    if not bool(row["confirmed"]):
        return "unconfirmed", row["source"], "当日前复权日 K 尚未确认"
    return "confirmed", row["source"], None


def audit_market_data(
    database: Database,
    stock_codes: Iterable[str],
    session_date: date,
    as_of_time: datetime,
) -> AuditReport:
    """生成只读体检报告；即使发现数据异常也不会修改任何表。"""
    codes = tuple(dict.fromkeys(stock_codes))
    if not codes:
        raise ValueError("行情体检至少需要一个股票代码。")
    target = _validate_as_of(session_date, as_of_time)
    session_key = session_date.isoformat()
    target_key = target.isoformat()
    expected = [point for point in session_minutes(session_date) if point <= target]
    expected_keys = {point.isoformat() for point in expected}

    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        calendar = connection.execute(
            "SELECT is_open, source FROM trading_calendar WHERE trade_date = ?",
            (session_key,),
        ).fetchone()
        calendar_errors = int(connection.execute(
            """
            SELECT COUNT(*) FROM data_quality_issues
            WHERE data_kind = 'calendar' AND data_time = ? AND severity = 'error'
            """,
            (session_key,),
        ).fetchone()[0])
        calendar_source = None if calendar is None else calendar["source"]
        if calendar is None or calendar_errors:
            calendar_status = "unknown"
        elif bool(calendar["is_open"]):
            calendar_status = "open"
        else:
            calendar_status = "closed"

        reports: list[CodeAudit] = []
        for code in codes:
            rows = list(connection.execute(
                """
                SELECT bar_time, confirmed, source FROM minute_bars
                WHERE stock_code = ? AND bar_time >= ? AND bar_time <= ?
                ORDER BY bar_time
                """,
                (code, expected[0].isoformat(), target_key),
            ))
            by_time = {row["bar_time"]: row for row in rows}
            missing = tuple(sorted(expected_keys - set(by_time)))
            confirmed = sum(bool(row["confirmed"]) for row in rows)
            unconfirmed = len(rows) - confirmed
            target_row = by_time.get(target_key)
            error_count, target_errors = _quality_counts(
                connection, code, expected[0].isoformat(), target_key
            )
            checkpoint = connection.execute(
                """
                SELECT last_contiguous_time, status FROM monitor_code_states
                WHERE session_date = ? AND stock_code = ?
                """,
                (session_key, code),
            ).fetchone()
            daily_status, daily_source, daily_reason = _daily_audit(
                connection, code, session_key
            )
            target_confirmed = (
                None if target_row is None else bool(target_row["confirmed"])
            )
            price_line = (
                calendar_status != "closed"
                and target_row is not None
                and bool(target_row["confirmed"])
                and target_errors == 0
            )
            reasons: list[str] = []
            if calendar_status == "unknown":
                reasons.append("交易日历缺失或存在冲突，不能确认当日开市状态")
            if calendar_status == "closed":
                reasons.append("交易日历明确当日休市")
            if missing:
                reasons.append(f"截至目标分钟缺少 {len(missing)} 根一分钟记录")
            if unconfirmed:
                reasons.append(f"有 {unconfirmed} 根已保存分钟尚未确认")
            if error_count:
                reasons.append(f"当日关联质量错误 {error_count} 条")
            if target_row is None:
                reasons.append("目标分钟缺失，价格线未知")
            elif not bool(target_row["confirmed"]):
                reasons.append("目标分钟尚未确认，价格线未知")
            elif target_errors:
                reasons.append("目标分钟存在质量冲突，价格线未知")
            if daily_reason:
                reasons.append(daily_reason)

            if calendar_status == "closed":
                status = "unknown"
            elif not missing and unconfirmed == 0 and error_count == 0 and calendar_status == "open":
                status = "ok"
            elif price_line:
                status = "price_only"
            else:
                status = "unknown"
            reports.append(CodeAudit(
                stock_code=code,
                status=status,
                expected_minutes=len(expected),
                saved_minutes=len(rows),
                confirmed_minutes=confirmed,
                unconfirmed_minutes=unconfirmed,
                missing_times=missing,
                latest_minute_time=rows[-1]["bar_time"] if rows else None,
                target_minute_time=None if target_row is None else target_row["bar_time"],
                target_confirmed=target_confirmed,
                price_line_available=price_line,
                quality_error_count=error_count,
                sources=tuple(sorted({row["source"] for row in rows})),
                checkpoint_time=None if checkpoint is None else checkpoint["last_contiguous_time"],
                checkpoint_status=None if checkpoint is None else checkpoint["status"],
                daily_status=daily_status,
                daily_source=daily_source,
                reasons=tuple(reasons),
            ))

    overall = "ok" if all(item.status == "ok" for item in reports) else "unknown"
    return AuditReport(
        session_date, target, calendar_status, calendar_source, overall, tuple(reports)
    )
