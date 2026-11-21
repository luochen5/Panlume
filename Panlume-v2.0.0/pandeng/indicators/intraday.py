"""盘中指标的确定性计算；不访问网络，不调用模型。"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from pandeng.data.schedule import CHINA_TZ, is_session_minute, session_minutes
from pandeng.storage.db import Database


@dataclass(frozen=True)
class IntradayIndicators:
    stock_code: str
    target_time: datetime
    status: str
    values: dict[str, float | int | None]
    unknown_reasons: tuple[str, ...]
    latest_minute_bar_id: int | None
    source_bar_ids: tuple[int, ...]


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values)


def _pct(current: float, reference: float) -> float | None:
    if reference <= 0:
        return None
    return (current / reference - 1.0) * 100.0


def _rows(
    connection: sqlite3.Connection, code: str, day: str, target: datetime
) -> list[sqlite3.Row]:
    return list(connection.execute(
        """
        SELECT * FROM minute_bars
        WHERE stock_code = ? AND substr(bar_time, 1, 10) = ? AND bar_time <= ?
        ORDER BY bar_time
        """,
        (code, day, target.isoformat()),
    ))


def _continuous(rows: list[sqlite3.Row], target: datetime) -> tuple[bool, str | None]:
    expected = [point for point in session_minutes(target.date()) if point <= target]
    actual = [datetime.fromisoformat(row["bar_time"]).astimezone(CHINA_TZ) for row in rows]
    if actual != expected:
        missing = [point.isoformat() for point in expected if point not in set(actual)]
        detail = ", ".join(missing[:3]) if missing else "存在额外或乱序分钟"
        return False, f"当日分钟序列不连续：{detail}。"
    unconfirmed = [row["bar_time"] for row in rows if not bool(row["confirmed"])]
    if unconfirmed:
        return False, f"分钟线尚未确认：{unconfirmed[-1]}。"
    return True, None


def calculate_intraday(
    database: Database,
    stock_code: str,
    target_time: datetime,
    *,
    sector_code: str | None = None,
    breakout_lookback: int = 20,
) -> IntradayIndicators:
    """计算当前时刻可见的盘中指标。

    指标预热不足只让对应字段为 None。目标分钟有效而历史序列有缺口时，
    保留当前价格线供固定止损使用，完整上下文指标保持未知。
    目标分钟缺失或未确认时，整份快照为 unknown。
    """
    target = target_time.astimezone(CHINA_TZ)
    if not is_session_minute(target):
        raise ValueError("指标目标时间必须是带时区的 A 股交易分钟。")
    if breakout_lookback < 1:
        raise ValueError("突破回看窗口必须大于 0。")
    day = target.date().isoformat()
    reasons: list[str] = []
    values: dict[str, float | int | None] = {
        "close": None,
        "volume": None,
        "return_1m_pct": None,
        "return_5m_pct": None,
        "volume_ratio": None,
        "vwap_estimate": None,
        "day_high": None,
        "day_low": None,
        "ma5": None,
        "ma20_minute": None,
        "relative_sector_pct": None,
        "session_return_pct": None,
        "prior_breakout_high": None,
    }
    with database.connect() as connection:
        rows = _rows(connection, stock_code, day, target)
        latest_id = int(rows[-1]["id"]) if rows else None
        current = next(
            (row for row in reversed(rows) if row["bar_time"] == target.isoformat()),
            None,
        )
        if current is None:
            return IntradayIndicators(
                stock_code, target, "unknown", values, ("缺少目标时刻分钟线。",),
                latest_id, tuple(sorted(int(row["id"]) for row in rows)),
            )
        if not bool(current["confirmed"]):
            return IntradayIndicators(
                stock_code, target, "unknown", values, ("目标时刻分钟线尚未确认。",),
                int(current["id"]), (int(current["id"]),),
            )

        values.update({
            "close": float(current["close"]),
            "volume": int(current["volume"]),
        })
        continuous, continuity_reason = _continuous(rows, target)
        if not continuous:
            reasons.append(continuity_reason or "当日分钟状态未知。")
            reasons.append(
                "历史分钟序列不完整：仅当前收盘价和成交量有效，"
                "依赖完整当日序列的指标均保持未知；固定止损仍可独立判断。"
            )
            return IntradayIndicators(
                stock_code, target, "ok", values, tuple(reasons),
                int(current["id"]), (int(current["id"]),),
            )

        source_ids = {int(row["id"]) for row in rows}
        closes = [float(row["close"]) for row in rows]
        values.update({
            "day_high": max(float(row["high"]) for row in rows),
            "day_low": min(float(row["low"]) for row in rows),
            "session_return_pct": _pct(closes[-1], float(rows[0]["open"])),
        })

        for periods, key in ((1, "return_1m_pct"), (5, "return_5m_pct")):
            if len(closes) >= periods + 1 and closes[-periods - 1] > 0:
                values[key] = _pct(closes[-1], closes[-periods - 1])
            else:
                reasons.append(f"{key} 需要当前及此前 {periods} 个连续交易分钟。")
        for periods, key in ((5, "ma5"), (20, "ma20_minute")):
            if len(closes) >= periods:
                values[key] = _mean(closes[-periods:])
            else:
                reasons.append(f"{key} 至少需要 {periods} 根当日分钟线。")
        if len(rows) > breakout_lookback:
            values["prior_breakout_high"] = max(
                float(row["high"]) for row in rows[-breakout_lookback - 1:-1]
            )
        else:
            reasons.append(f"放量突破需要此前 {breakout_lookback} 根分钟线，不含当前分钟。")

        total_volume = sum(int(row["volume"]) for row in rows)
        if total_volume > 0:
            typical_volume = math.fsum(
                ((float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0)
                * int(row["volume"])
                for row in rows
            )
            values["vwap_estimate"] = typical_volume / total_volume
        else:
            reasons.append("当日累计成交量为 0，典型价估算 VWAP 未知。")

        previous_days = [row["trade_date"] for row in connection.execute(
            """
            SELECT trade_date FROM trading_calendar
            WHERE trade_date < ? AND is_open = 1
            ORDER BY trade_date DESC LIMIT 20
            """,
            (day,),
        )]
        if len(previous_days) < 20:
            reasons.append("成交量比需要此前 20 个明确开市的交易日。")
        else:
            oldest = previous_days[-1]
            calendar_rows = list(connection.execute(
                """
                SELECT trade_date, is_open FROM trading_calendar
                WHERE trade_date >= ? AND trade_date < ? ORDER BY trade_date
                """,
                (oldest, day),
            ))
            expected_dates = []
            cursor = date.fromisoformat(oldest)
            while cursor.isoformat() < day:
                expected_dates.append(cursor.isoformat())
                cursor = date.fromordinal(cursor.toordinal() + 1)
            calendar_complete = [row["trade_date"] for row in calendar_rows] == expected_dates
            open_count = sum(bool(row["is_open"]) for row in calendar_rows)
            if not calendar_complete or open_count != 20:
                reasons.append("成交量基线区间的交易日历不完整，不能用更早日历替代缺失日期。")
            else:
                placeholders = ",".join("?" for _ in previous_days)
                baseline_rows = list(connection.execute(
                    f"""
                    SELECT * FROM minute_bars
                    WHERE stock_code = ? AND substr(bar_time, 1, 10) IN ({placeholders})
                      AND substr(bar_time, 12, 5) = ?
                    """,
                    (stock_code, *previous_days, target.strftime("%H:%M")),
                ))
                by_day = {row["bar_time"][:10]: row for row in baseline_rows}
                if all(
                    day_key in by_day and bool(by_day[day_key]["confirmed"])
                    for day_key in previous_days
                ):
                    baseline_volumes = [int(by_day[day_key]["volume"]) for day_key in previous_days]
                    baseline_mean = _mean([float(value) for value in baseline_volumes])
                    if baseline_mean > 0:
                        values["volume_ratio"] = int(current["volume"]) / baseline_mean
                        source_ids.update(int(row["id"]) for row in baseline_rows)
                    else:
                        reasons.append("同时刻历史平均成交量为 0，成交量比未知。")
                else:
                    reasons.append("此前 20 个开市日中，同一时刻的已确认分钟成交量不完整。")

        if not sector_code:
            reasons.append("未配置 sector_code，相对板块表现未知。")
        else:
            sector_rows = _rows(connection, sector_code, day, target)
            sector_continuous, sector_continuity_reason = _continuous(sector_rows, target)
            sector_by_time = {row["bar_time"]: row for row in sector_rows}
            first_time = rows[0]["bar_time"]
            sector_first = sector_by_time.get(first_time)
            sector_current = sector_by_time.get(target.isoformat())
            if not sector_continuous:
                reasons.append(f"板块{sector_continuity_reason or '当日分钟状态未知。'}")
            elif (
                sector_first is not None and sector_current is not None
                and float(sector_first["open"]) > 0 and float(rows[0]["open"]) > 0
            ):
                stock_return = closes[-1] / float(rows[0]["open"]) - 1.0
                sector_return = (
                    float(sector_current["close"]) / float(sector_first["open"]) - 1.0
                )
                values["relative_sector_pct"] = (stock_return - sector_return) * 100.0
                # 整段板块分钟都参与了连续性门槛，保留其 ID 才能完整复查本次判定。
                source_ids.update(int(row["id"]) for row in sector_rows)
            else:
                reasons.append("板块首分钟开盘价或股票首分钟开盘价缺失、无效。")

    return IntradayIndicators(
        stock_code, target, "ok", values, tuple(reasons), latest_id, tuple(sorted(source_ids))
    )
