"""确定性 MACD 与 Wilder RSI；只读同源、同周期、同复权数据。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from pandeng.config.schema import MomentumConfig
from pandeng.data.schedule import session_minutes
from pandeng.storage.db import Database


@dataclass(frozen=True)
class MacdResult:
    fast_ema: float
    slow_ema: float
    macd: float
    signal: float
    histogram: float


@dataclass(frozen=True)
class MomentumInput:
    record_id: int
    timestamp: str
    close: float
    stock_code: str
    timeframe: str
    adjustment: str


@dataclass(frozen=True)
class MomentumResult:
    status: str
    stock_code: str
    timeframe: str
    adjustment: str
    parameters: dict[str, int]
    input_range: dict[str, Any]
    sample_count: int
    macd: MacdResult | None
    rsi: float | None
    source_record_ids: tuple[int, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stock_code": self.stock_code,
            "timeframe": self.timeframe,
            "adjustment": self.adjustment,
            "parameters": dict(self.parameters),
            "input_range": dict(self.input_range),
            "sample_count": self.sample_count,
            "macd": None if self.macd is None else {
                "fast_ema": self.macd.fast_ema,
                "slow_ema": self.macd.slow_ema,
                "macd": self.macd.macd,
                "signal": self.macd.signal,
                "histogram": self.macd.histogram,
            },
            "rsi": self.rsi,
            "source_record_ids": list(self.source_record_ids),
            "reason": self.reason,
        }


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(result[-1] + alpha * (float(value) - result[-1]))
    return result


def calculate_macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> MacdResult | None:
    if fast < 1 or slow <= fast or signal < 1:
        raise ValueError("MACD 参数必须满足 slow > fast >= 1 且 signal >= 1。")
    required = slow + signal - 1
    if len(closes) < required:
        return None
    fast_line = _ema(closes, fast)
    slow_line = _ema(closes, slow)
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast_line, slow_line)]
    signal_line = _ema(macd_line, signal)
    return MacdResult(
        fast_ema=fast_line[-1], slow_ema=slow_line[-1], macd=macd_line[-1],
        signal=signal_line[-1], histogram=macd_line[-1] - signal_line[-1],
    )


def calculate_rsi(closes: Sequence[float], period: int = 14) -> float | None:
    if period < 1:
        raise ValueError("RSI 周期必须大于 0。")
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for previous, current in zip(closes, closes[1:]):
        change = float(current) - float(previous)
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    average_gain = math.fsum(gains[:period]) / period
    average_loss = math.fsum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    if average_gain == 0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def calculate_momentum_records(
    records: Sequence[MomentumInput],
    config: MomentumConfig = MomentumConfig(),
    *,
    expected_timestamps: Sequence[str] | None = None,
) -> MomentumResult:
    parameters = {
        "macd_fast": config.macd_fast, "macd_slow": config.macd_slow,
        "macd_signal": config.macd_signal, "rsi_period": config.rsi_period,
    }
    if not records:
        return MomentumResult(
            "insufficient_data", "", "", "", parameters,
            {"start": None, "end": None}, 0, None, None, (), "数据不足：没有有效记录。",
        )
    first = records[0]
    result_base = {
        "stock_code": first.stock_code, "timeframe": first.timeframe,
        "adjustment": first.adjustment, "parameters": parameters,
        "input_range": {"start": first.timestamp, "end": records[-1].timestamp},
        "sample_count": len(records),
        "source_record_ids": tuple(item.record_id for item in records),
    }
    invalid = ""
    expected_adjustment = "none" if first.timeframe == "minute" else "qfq" if first.timeframe == "daily" else ""
    if not expected_adjustment:
        invalid = "时间周期必须是 minute 或 daily。"
    if any(
        item.stock_code != first.stock_code or item.timeframe != first.timeframe
        or item.adjustment != first.adjustment or item.adjustment != expected_adjustment
        for item in records
    ):
        invalid = "输入混入不同股票、时间周期或复权口径。"
    timestamps = [item.timestamp for item in records]
    if len(set(timestamps)) != len(timestamps) or timestamps != sorted(timestamps):
        invalid = "输入时间存在重复或乱序。"
    if len({item.record_id for item in records}) != len(records):
        invalid = "输入证据记录 ID 存在重复。"
    if any(
        isinstance(item.close, bool) or not isinstance(item.close, (int, float))
        or not math.isfinite(float(item.close)) or float(item.close) <= 0
        for item in records
    ):
        invalid = "收盘价存在缺失、非有限值或非正值。"
    if expected_timestamps is not None and timestamps != list(expected_timestamps):
        invalid = "输入时间序列存在缺失或额外记录，不能跨缺口计算。"
    if invalid:
        return MomentumResult("invalid_data", macd=None, rsi=None, reason=invalid, **result_base)
    closes = [float(item.close) for item in records]
    macd = calculate_macd(
        closes, config.macd_fast, config.macd_slow, config.macd_signal
    )
    rsi = calculate_rsi(closes, config.rsi_period)
    status = "ok" if macd is not None and rsi is not None else "insufficient_data"
    required = max(config.macd_slow + config.macd_signal - 1, config.rsi_period + 1)
    reason = "" if status == "ok" else f"数据不足：完整计算至少需要 {required} 个有效样本，当前 {len(records)} 个。"
    return MomentumResult(status, macd=macd, rsi=rsi, reason=reason, **result_base)


def calculate_momentum(
    database: Database,
    stock_code: str,
    timeframe: str,
    through: datetime | date,
    config: MomentumConfig = MomentumConfig(),
) -> MomentumResult:
    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        if timeframe == "minute":
            if not isinstance(through, datetime):
                raise ValueError("分钟指标目标必须是带时区时间。")
            target = through.isoformat()
            day = through.date()
            rows = list(connection.execute(
                """
                SELECT id, bar_time, close, stock_code, adjustment FROM minute_bars
                WHERE stock_code = ? AND substr(bar_time, 1, 10) = ? AND bar_time <= ?
                  AND confirmed = 1
                ORDER BY bar_time, id
                """,
                (stock_code, day.isoformat(), target),
            ))
            expected = [point.isoformat() for point in session_minutes(day) if point.isoformat() <= target]
        elif timeframe == "daily":
            if not isinstance(through, date):
                raise ValueError("日线指标目标必须是交易日日期。")
            rows = list(connection.execute(
                """
                SELECT id, trade_date, close, stock_code, adjustment FROM daily_qfq_bars
                WHERE stock_code = ? AND trade_date <= ? AND confirmed = 1
                ORDER BY trade_date, id
                """,
                (stock_code, through.isoformat()),
            ))
            expected = None
            if rows:
                calendar = [row["trade_date"] for row in connection.execute(
                    """
                    SELECT trade_date FROM trading_calendar
                    WHERE is_open = 1 AND trade_date BETWEEN ? AND ? ORDER BY trade_date
                    """,
                    (rows[0]["trade_date"], through.isoformat()),
                )]
                expected = calendar
        else:
            raise ValueError("指标时间周期必须是 minute 或 daily。")
    records = [
        MomentumInput(
            int(row["id"]), row["bar_time"] if timeframe == "minute" else row["trade_date"],
            row["close"], row["stock_code"], timeframe, row["adjustment"],
        )
        for row in rows
    ]
    return calculate_momentum_records(records, config, expected_timestamps=expected)
