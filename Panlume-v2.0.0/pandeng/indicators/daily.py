"""确认前复权日 K 的 MA20、拐头天数与正式信号。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone

from pandeng.storage.db import Database


@dataclass(frozen=True)
class DailyAnalysisResult:
    stock_code: str
    trade_date: date
    status: str
    ma20: float | None
    previous_ma20: float | None
    turn_days: int | None
    close: float | None
    formal_signal: bool | None
    analysis_mode: str
    is_backfill: bool
    reason: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(
    database: Database,
    *,
    stock_code: str,
    day: date,
    daily_bar_id: int,
    status: str,
    ma20: float | None,
    previous_ma20: float | None,
    turn_days: int | None,
    close: float | None,
    formal_signal: bool | None,
    reason: str | None,
    source_ids: list[int],
    analysis_mode: str,
    is_backfill: bool,
) -> DailyAnalysisResult:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO daily_analysis(
                stock_code, trade_date, daily_bar_id, status, ma20, previous_ma20,
                turn_days, close, formal_signal, analysis_mode, is_backfill,
                unknown_reason, source_daily_bar_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                daily_bar_id = excluded.daily_bar_id,
                status = excluded.status,
                ma20 = excluded.ma20,
                previous_ma20 = excluded.previous_ma20,
                turn_days = excluded.turn_days,
                close = excluded.close,
                formal_signal = excluded.formal_signal,
                unknown_reason = excluded.unknown_reason,
                source_daily_bar_ids_json = excluded.source_daily_bar_ids_json
            """,
            (
                stock_code, day.isoformat(), daily_bar_id, status, ma20, previous_ma20,
                turn_days, close,
                None if formal_signal is None else int(formal_signal),
                analysis_mode, int(is_backfill), reason,
                json.dumps(source_ids), _now(),
            ),
        )
    return DailyAnalysisResult(
        stock_code, day, status, ma20, previous_ma20, turn_days, close,
        formal_signal, analysis_mode, is_backfill, reason or "",
    )


def analyze_daily(
    database: Database,
    stock_code: str,
    through_date: date,
    *,
    analysis_mode: str = "replay",
) -> list[DailyAnalysisResult]:
    """重算截至指定日期的已确认日线序列，并保留补记标记。

    首次可计算 MA20 时没有前一日 MA20，拐头未知；下一次可比较时，
    不下降记为 1，下降记为 0，之后按相邻 MA20 递增或归零。
    """
    if analysis_mode not in {"live", "offline_recovery", "replay"}:
        raise ValueError("无效的日线分析模式。")
    with database.connect() as connection:
        rows = list(connection.execute(
            """
            SELECT * FROM daily_qfq_bars
            WHERE stock_code = ? AND trade_date <= ?
            ORDER BY trade_date
            """,
            (stock_code, through_date.isoformat()),
        ))
    if not rows:
        return []

    valid: list = []
    previous_ma: float | None = None
    previous_turn: int | None = None
    broken_reason: str | None = None
    results: list[DailyAnalysisResult] = []
    for row in rows:
        day = date.fromisoformat(row["trade_date"])
        backfill = analysis_mode != "live" or day < through_date
        common = {
            "stock_code": stock_code,
            "day": day,
            "daily_bar_id": int(row["id"]),
            "close": float(row["close"]),
            "analysis_mode": analysis_mode,
            "is_backfill": backfill,
        }
        if not bool(row["confirmed"]):
            broken_reason = f"{day.isoformat()} 日 K 未确认，后续正式序列保持未知。"
            results.append(_save(
                database, **common, status="unknown", ma20=None, previous_ma20=None,
                turn_days=None, formal_signal=None, reason="日 K 尚未确认，不能生成正式信号。",
                source_ids=[int(row["id"])],
            ))
            previous_ma = None
            previous_turn = None
            continue
        if broken_reason is not None:
            results.append(_save(
                database, **common, status="unknown", ma20=None, previous_ma20=None,
                turn_days=None, formal_signal=None, reason=broken_reason,
                source_ids=[int(row["id"])],
            ))
            continue

        valid.append(row)
        source_ids = [int(item["id"]) for item in valid[-21:]]
        if len(valid) < 20:
            results.append(_save(
                database, **common, status="unknown", ma20=None, previous_ma20=None,
                turn_days=None, formal_signal=None,
                reason=f"只有 {len(valid)} 根连续已确认前复权日 K，MA20 需要 20 根。",
                source_ids=source_ids,
            ))
            continue

        ma20 = math.fsum(float(item["close"]) for item in valid[-20:]) / 20.0
        if previous_ma is None:
            result = _save(
                database, **common, status="unknown", ma20=ma20, previous_ma20=None,
                turn_days=None, formal_signal=None,
                reason="首个 MA20 没有前一日 MA20，拐头天数未知，暂不能生成正式信号。",
                source_ids=source_ids,
            )
            previous_turn = None
        else:
            turn_days = 0 if ma20 < previous_ma else (
                1 if previous_turn is None else previous_turn + 1
            )
            signal = float(row["close"]) > ma20 and turn_days >= 3
            result = _save(
                database, **common, status="ok", ma20=ma20, previous_ma20=previous_ma,
                turn_days=turn_days, formal_signal=signal, reason=None,
                source_ids=source_ids,
            )
            previous_turn = turn_days
        previous_ma = ma20
        results.append(result)
    return results
