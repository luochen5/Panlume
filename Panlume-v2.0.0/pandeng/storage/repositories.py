"""行情、质量问题和检查点的 SQLite 读写。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from pandeng.data.base import AdapterRecord, DailyBar, MinuteBar, TradingDay
from pandeng.data.cleaner import (
    QualityIssue,
    minute_gap_issues,
    normalize_daily,
    normalize_minute,
    normalize_trading_day,
    raw_mapping,
    raw_to_json,
)

from .db import Database


@dataclass(frozen=True)
class IngestionSummary:
    run_id: int
    data_kind: str
    status: str
    received: int
    accepted: int
    duplicates: int
    rejected: int
    unknown_codes: tuple[str, ...]

    @property
    def status_text(self) -> str:
        return "正常" if self.status == "ok" else "状态未知"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_run(
    connection: sqlite3.Connection, kind: str, adapter: str, replay_clock: datetime | None
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO ingestion_runs(data_kind, adapter, status, replay_clock, created_at)
        VALUES (?, ?, 'unknown', ?, ?)
        """,
        (kind, adapter, None if replay_clock is None else replay_clock.isoformat(), _now()),
    )
    return int(cursor.lastrowid)


def _save_issue(
    connection: sqlite3.Connection,
    run_id: int,
    record_index: int | None,
    issue: QualityIssue,
) -> None:
    connection.execute(
        """
        INSERT INTO data_quality_issues(
            run_id, record_index, data_kind, stock_code, data_time,
            issue_type, severity, message, raw_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            record_index,
            issue.data_kind,
            issue.stock_code,
            issue.data_time,
            issue.issue_type,
            issue.severity,
            issue.message,
            issue.raw_json,
            _now(),
        ),
    )


def _finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    status: str,
    received: int,
    accepted: int,
    duplicates: int,
    rejected: int,
) -> None:
    connection.execute(
        """
        UPDATE ingestion_runs
        SET status = ?, received_count = ?, accepted_count = ?,
            duplicate_count = ?, rejected_count = ?
        WHERE id = ?
        """,
        (status, received, accepted, duplicates, rejected, run_id),
    )


def _checkpoint(
    connection: sqlite3.Connection,
    stream_key: str,
    data_time: str | None,
    run_id: int,
    status: str,
) -> None:
    connection.execute(
        """
        INSERT INTO processing_checkpoints(
            stream_key, last_data_time, last_run_id, status, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(stream_key) DO UPDATE SET
            last_data_time = COALESCE(excluded.last_data_time, last_data_time),
            last_run_id = excluded.last_run_id,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (stream_key, data_time, run_id, status, _now()),
    )


def ingest_minutes(
    database: Database,
    records: Iterable[AdapterRecord],
    *,
    adapter: str,
    replay_clock: datetime | None = None,
    check_gaps: bool = True,
    allow_unconfirmed_revisions: bool = False,
) -> IngestionSummary:
    items = list(records)
    with database.connect() as connection:
        run_id = _start_run(connection, "minute", adapter, replay_clock)
        prepared = [
            (index, item, normalize_minute(item, default_source=adapter, replay_clock=replay_clock))
            for index, item in enumerate(items)
        ]
        previous_by_code: dict[str, MinuteBar | None] = {}
        for _, _, result in prepared:
            bar = result.record
            if isinstance(bar, MinuteBar) and bar.stock_code not in previous_by_code:
                row = connection.execute(
                    "SELECT * FROM minute_bars WHERE stock_code = ? "
                    "ORDER BY bar_time DESC LIMIT 1",
                    (bar.stock_code,),
                ).fetchone()
                previous_by_code[bar.stock_code] = _row_to_minute(row) if row else None

        accepted = duplicates = rejected = 0
        has_error = False
        unknown_codes: set[str] = set()
        new_by_code: dict[str, list[MinuteBar]] = {}
        latest_by_code: dict[str, str] = {}

        for index, item, result in prepared:
            raw = raw_mapping(item)
            for issue in result.issues:
                _save_issue(connection, run_id, index, issue)
                if issue.severity == "error":
                    has_error = True
                    if issue.stock_code:
                        unknown_codes.add(issue.stock_code)
            bar = result.record
            if not isinstance(bar, MinuteBar):
                rejected += 1
                continue

            existing = connection.execute(
                "SELECT * FROM minute_bars WHERE stock_code = ? AND bar_time = ?",
                (bar.stock_code, bar.bar_time.isoformat()),
            ).fetchone()
            if existing is not None:
                if _minute_matches(existing, bar):
                    duplicates += 1
                    issue = QualityIssue(
                        data_kind="minute",
                        issue_type="exact_duplicate",
                        severity="info",
                        message="完全相同的分钟记录已去重。",
                        raw_json=raw_to_json(raw),
                        stock_code=bar.stock_code,
                        data_time=bar.bar_time.isoformat(),
                    )
                    _save_issue(connection, run_id, index, issue)
                elif _minute_confirmation_promotable(existing, bar):
                    before_raw = existing["raw_json"]
                    connection.execute(
                        """
                        UPDATE minute_bars
                        SET confirmed = 1, ingestion_run_id = ?, raw_json = ?
                        WHERE id = ? AND confirmed = 0
                        """,
                        (run_id, raw_to_json(raw), existing["id"]),
                    )
                    _save_issue(
                        connection,
                        run_id,
                        index,
                        QualityIssue(
                            data_kind="minute",
                            issue_type="confirmation_promoted",
                            severity="info",
                            message=(
                                "OHLC、成交量、来源和复权口径均未变化，"
                                "仅将分钟从未确认推进为已确认。"
                            ),
                            raw_json=json.dumps(
                                {"before": json.loads(before_raw), "after": raw},
                                ensure_ascii=False,
                                default=str,
                            ),
                            stock_code=bar.stock_code,
                            data_time=bar.bar_time.isoformat(),
                        ),
                    )
                elif (
                    allow_unconfirmed_revisions
                    and not bool(existing["confirmed"])
                    and not bar.confirmed
                    and existing["source"] == bar.source
                    and existing["adjustment"] == bar.adjustment
                ):
                    before = {
                        key: existing[key]
                        for key in (
                            "stock_code", "bar_time", "open", "high", "low", "close",
                            "volume", "source", "adjustment", "confirmed", "raw_json",
                        )
                    }
                    before["confirmed"] = bool(before["confirmed"])
                    before["raw_json"] = json.loads(before["raw_json"])
                    after = {
                        "stock_code": bar.stock_code,
                        "bar_time": bar.bar_time.isoformat(),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "source": bar.source,
                        "adjustment": bar.adjustment,
                        "confirmed": bar.confirmed,
                        "raw_json": raw,
                    }
                    connection.execute(
                        """
                        UPDATE minute_bars
                        SET open = ?, high = ?, low = ?, close = ?, volume = ?,
                            ingestion_run_id = ?, raw_json = ?
                        WHERE id = ? AND confirmed = 0
                        """,
                        (
                            bar.open, bar.high, bar.low, bar.close, bar.volume,
                            run_id, raw_to_json(raw), existing["id"],
                        ),
                    )
                    accepted += 1
                    _save_issue(
                        connection,
                        run_id,
                        index,
                        QualityIssue(
                            data_kind="minute",
                            issue_type="unconfirmed_revision",
                            severity="info",
                            message="同一未确认分钟发生修订；前值和后值已保留，当前快照已更新。",
                            raw_json=json.dumps(
                                {"before": before, "after": after},
                                ensure_ascii=False,
                                default=str,
                            ),
                            stock_code=bar.stock_code,
                            data_time=bar.bar_time.isoformat(),
                        ),
                    )
                else:
                    rejected += 1
                    has_error = True
                    unknown_codes.add(bar.stock_code)
                    issue = QualityIssue(
                        data_kind="minute",
                        issue_type="conflicting_duplicate",
                        severity="error",
                        message="同一代码和时间的分钟记录内容冲突，状态未知。",
                        raw_json=raw_to_json(raw),
                        stock_code=bar.stock_code,
                        data_time=bar.bar_time.isoformat(),
                    )
                    _save_issue(connection, run_id, index, issue)
                continue

            cursor = connection.execute(
                """
                INSERT INTO minute_bars(
                    stock_code, bar_time, open, high, low, close, volume,
                    source, adjustment, confirmed, ingestion_run_id, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bar.stock_code,
                    bar.bar_time.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.source,
                    bar.adjustment,
                    int(bar.confirmed),
                    run_id,
                    raw_to_json(raw),
                    _now(),
                ),
            )
            if cursor.rowcount == 1:
                accepted += 1
                new_by_code.setdefault(bar.stock_code, []).append(bar)
                latest_by_code[bar.stock_code] = max(
                    latest_by_code.get(bar.stock_code, bar.bar_time.isoformat()),
                    bar.bar_time.isoformat(),
                )

        if check_gaps:
            for code, bars in new_by_code.items():
                for issue in minute_gap_issues(bars, previous_by_code.get(code)):
                    _save_issue(connection, run_id, None, issue)
                    has_error = True
                    unknown_codes.add(code)

        # 有错误记录时，即使有效记录齐全，也必须让对应代码保持未知。
        for code in previous_by_code:
            if connection.execute(
                """
                SELECT 1 FROM data_quality_issues
                WHERE run_id = ? AND stock_code = ? AND severity = 'error'
                LIMIT 1
                """,
                (run_id, code),
            ).fetchone():
                unknown_codes.add(code)

        for code in set(previous_by_code) | unknown_codes:
            previous = previous_by_code.get(code)
            data_time = latest_by_code.get(code)
            if data_time is None and previous is not None:
                data_time = previous.bar_time.isoformat()
            _checkpoint(
                connection,
                f"minute:{code}",
                data_time,
                run_id,
                "unknown" if code in unknown_codes else "ok",
            )

        status = "unknown" if has_error else "ok"
        _finish_run(connection, run_id, status, len(items), accepted, duplicates, rejected)

    return IngestionSummary(
        run_id, "minute", status, len(items), accepted, duplicates, rejected,
        tuple(sorted(unknown_codes)),
    )


def _row_to_minute(row: sqlite3.Row) -> MinuteBar:
    return MinuteBar(
        stock_code=row["stock_code"],
        bar_time=datetime.fromisoformat(row["bar_time"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        source=row["source"],
        adjustment=row["adjustment"],
        confirmed=bool(row["confirmed"]),
    )


def _minute_confirmation_promotable(row: sqlite3.Row, bar: MinuteBar) -> bool:
    """只允许 confirmed=false → true，其他已保存字段必须完全一致。"""
    return (
        not bool(row["confirmed"])
        and bar.confirmed
        and row["open"] == bar.open
        and row["high"] == bar.high
        and row["low"] == bar.low
        and row["close"] == bar.close
        and row["volume"] == bar.volume
        and row["source"] == bar.source
        and row["adjustment"] == bar.adjustment
    )


def _minute_matches(row: sqlite3.Row, bar: MinuteBar) -> bool:
    return (
        row["open"] == bar.open
        and row["high"] == bar.high
        and row["low"] == bar.low
        and row["close"] == bar.close
        and row["volume"] == bar.volume
        and row["source"] == bar.source
        and row["adjustment"] == bar.adjustment
        and bool(row["confirmed"]) == bar.confirmed
    )


def ingest_daily(
    database: Database, records: Iterable[AdapterRecord], *, adapter: str
) -> IngestionSummary:
    items = list(records)
    with database.connect() as connection:
        run_id = _start_run(connection, "daily_qfq", adapter, None)
        accepted = duplicates = rejected = 0
        has_error = False
        unknown_codes: set[str] = set()
        touched_codes: set[str] = set()
        latest_by_code: dict[str, str] = {}

        for index, item in enumerate(items):
            raw = raw_mapping(item)
            result = normalize_daily(item, default_source=adapter)
            for issue in result.issues:
                _save_issue(connection, run_id, index, issue)
                has_error = True
                if issue.stock_code:
                    unknown_codes.add(issue.stock_code)
            bar = result.record
            if not isinstance(bar, DailyBar):
                rejected += 1
                continue
            touched_codes.add(bar.stock_code)
            existing = connection.execute(
                "SELECT * FROM daily_qfq_bars WHERE stock_code = ? AND trade_date = ?",
                (bar.stock_code, bar.trade_date.isoformat()),
            ).fetchone()
            if existing is not None:
                if _daily_matches(existing, bar):
                    duplicates += 1
                    _save_issue(
                        connection,
                        run_id,
                        index,
                        QualityIssue(
                            "daily_qfq", "exact_duplicate", "info",
                            "完全相同的日线记录已去重。", raw_to_json(raw),
                            bar.stock_code, bar.trade_date.isoformat(),
                        ),
                    )
                elif _daily_confirmation_promotable(existing, bar):
                    before_raw = existing["raw_json"]
                    connection.execute(
                        """
                        UPDATE daily_qfq_bars
                        SET confirmed = 1, ingestion_run_id = ?, raw_json = ?
                        WHERE id = ? AND confirmed = 0
                        """,
                        (run_id, raw_to_json(raw), existing["id"]),
                    )
                    _save_issue(
                        connection, run_id, index,
                        QualityIssue(
                            "daily_qfq", "confirmation_promoted", "info",
                            "价格、来源和复权口径均未变化，仅将日 K 从未确认推进为已确认。",
                            json.dumps(
                                {"before": json.loads(before_raw), "after": raw},
                                ensure_ascii=False, default=str,
                            ),
                            bar.stock_code, bar.trade_date.isoformat(),
                        ),
                    )
                else:
                    rejected += 1
                    has_error = True
                    unknown_codes.add(bar.stock_code)
                    _save_issue(
                        connection,
                        run_id,
                        index,
                        QualityIssue(
                            "daily_qfq", "conflicting_duplicate", "error",
                            "同一代码和日期的前复权日线内容冲突，状态未知。",
                            raw_to_json(raw), bar.stock_code, bar.trade_date.isoformat(),
                        ),
                    )
                continue
            connection.execute(
                """
                INSERT INTO daily_qfq_bars(
                    stock_code, trade_date, open, high, low, close, volume,
                    source, adjustment, confirmed, ingestion_run_id, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bar.stock_code, bar.trade_date.isoformat(), bar.open, bar.high,
                    bar.low, bar.close, bar.volume, bar.source, bar.adjustment,
                    int(bar.confirmed), run_id, raw_to_json(raw), _now(),
                ),
            )
            accepted += 1
            latest_by_code[bar.stock_code] = max(
                latest_by_code.get(bar.stock_code, bar.trade_date.isoformat()),
                bar.trade_date.isoformat(),
            )

        for code in touched_codes | unknown_codes:
            row = connection.execute(
                "SELECT trade_date FROM daily_qfq_bars WHERE stock_code = ? "
                "ORDER BY trade_date DESC LIMIT 1",
                (code,),
            ).fetchone()
            data_time = latest_by_code.get(code) or (row["trade_date"] if row else None)
            _checkpoint(
                connection, f"daily_qfq:{code}", data_time, run_id,
                "unknown" if code in unknown_codes else "ok",
            )
        status = "unknown" if has_error else "ok"
        _finish_run(connection, run_id, status, len(items), accepted, duplicates, rejected)

    return IngestionSummary(
        run_id, "daily_qfq", status, len(items), accepted, duplicates, rejected,
        tuple(sorted(unknown_codes)),
    )


def _daily_confirmation_promotable(row: sqlite3.Row, bar: DailyBar) -> bool:
    """只允许 confirmed=false → true，其他已保存字段必须完全一致。"""
    return (
        not bool(row["confirmed"]) and bar.confirmed
        and row["open"] == bar.open and row["high"] == bar.high
        and row["low"] == bar.low and row["close"] == bar.close
        and row["volume"] == bar.volume and row["source"] == bar.source
        and row["adjustment"] == bar.adjustment
    )


def _daily_matches(row: sqlite3.Row, bar: DailyBar) -> bool:
    return (
        row["open"] == bar.open and row["high"] == bar.high
        and row["low"] == bar.low and row["close"] == bar.close
        and row["volume"] == bar.volume and row["source"] == bar.source
        and row["adjustment"] == bar.adjustment
        and bool(row["confirmed"]) == bar.confirmed
    )


def ingest_calendar(
    database: Database, records: Iterable[AdapterRecord], *, adapter: str
) -> IngestionSummary:
    items = list(records)
    with database.connect() as connection:
        run_id = _start_run(connection, "calendar", adapter, None)
        accepted = duplicates = rejected = 0
        unknown = False
        for index, item in enumerate(items):
            raw = raw_mapping(item)
            result = normalize_trading_day(item, default_source=adapter)
            for issue in result.issues:
                _save_issue(connection, run_id, index, issue)
                unknown = True
            day = result.record
            if not isinstance(day, TradingDay):
                rejected += 1
                continue
            existing = connection.execute(
                "SELECT * FROM trading_calendar WHERE trade_date = ?",
                (day.trade_date.isoformat(),),
            ).fetchone()
            if existing is not None:
                if (
                    bool(existing["is_open"]) == day.is_open
                    and existing["source"] == day.source
                ):
                    duplicates += 1
                else:
                    rejected += 1
                    unknown = True
                    _save_issue(
                        connection, run_id, index,
                        QualityIssue(
                            "calendar", "conflicting_duplicate", "error",
                            "同一日期的交易日历内容冲突，状态未知。", raw_to_json(raw),
                            data_time=day.trade_date.isoformat(),
                        ),
                    )
                continue
            connection.execute(
                """
                INSERT INTO trading_calendar(
                    trade_date, is_open, source, ingestion_run_id, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    day.trade_date.isoformat(), int(day.is_open), day.source,
                    run_id, raw_to_json(raw), _now(),
                ),
            )
            accepted += 1
        status = "unknown" if unknown else "ok"
        _finish_run(connection, run_id, status, len(items), accepted, duplicates, rejected)
    return IngestionSummary(run_id, "calendar", status, len(items), accepted, duplicates, rejected, ())


def recent_runs(database: Database, limit: int = 10) -> list[sqlite3.Row]:
    with database.connect() as connection:
        return list(
            connection.execute(
                "SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        )


def latest_minutes(database: Database) -> list[sqlite3.Row]:
    with database.connect() as connection:
        return list(
            connection.execute(
                """
                SELECT m.*
                FROM minute_bars AS m
                JOIN (
                    SELECT stock_code, MAX(bar_time) AS latest_time
                    FROM minute_bars GROUP BY stock_code
                ) AS latest
                ON m.stock_code = latest.stock_code AND m.bar_time = latest.latest_time
                ORDER BY m.stock_code
                """
            )
        )


def table_count(database: Database, table: str) -> int:
    allowed = {"minute_bars", "daily_qfq_bars", "trading_calendar", "data_quality_issues"}
    if table not in allowed:
        raise ValueError("不允许查询该表。")
    with database.connect() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
