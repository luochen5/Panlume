"""把适配器原始记录转换为可信数据，并明确记录异常。

清洗层只判断数据质量，不计算指标，也不产生任何交易判断。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from typing import Mapping

from .base import AdapterRecord, DailyBar, MinuteBar, RawRecord, TradingDay
from .schedule import CHINA_TZ, is_session_minute, previous_session_minute


@dataclass(frozen=True)
class QualityIssue:
    data_kind: str
    issue_type: str
    severity: str
    message: str
    raw_json: str
    stock_code: str | None = None
    data_time: str | None = None


@dataclass(frozen=True)
class CleanResult:
    record: MinuteBar | DailyBar | TradingDay | None
    issues: tuple[QualityIssue, ...]


def raw_mapping(record: AdapterRecord) -> RawRecord:
    if is_dataclass(record):
        return asdict(record)
    if isinstance(record, Mapping):
        return record
    raise TypeError("不支持的行情记录类型。")


def raw_to_json(record: AdapterRecord | RawRecord) -> str:
    return json.dumps(
        raw_mapping(record), ensure_ascii=False, sort_keys=True, default=str
    )


def _raw_datetime_text(raw: RawRecord, field: str) -> str | None:
    value = raw.get(field)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None if value is None else str(value)


def _issue(
    kind: str,
    raw: RawRecord,
    messages: list[str],
    *,
    time_field: str,
) -> tuple[QualityIssue, ...]:
    code_value = raw.get("stock_code")
    code = str(code_value) if code_value is not None else None
    data_time = _raw_datetime_text(raw, time_field)
    return tuple(
        QualityIssue(
            data_kind=kind,
            issue_type="invalid_record",
            severity="error",
            message=message,
            raw_json=raw_to_json(raw),
            stock_code=code,
            data_time=data_time,
        )
        for message in messages
    )


def _code(raw: RawRecord, errors: list[str]) -> str:
    value = raw.get("stock_code")
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value.strip()):
        errors.append("stock_code 必须是 6 位数字字符串。")
        return ""
    return value.strip()


def _source(raw: RawRecord, default: str, errors: list[str]) -> str:
    value = raw.get("source", default)
    if not isinstance(value, str) or not value.strip():
        errors.append("source 必须是非空字符串。")
        return default
    return value.strip()


def _number(raw: RawRecord, field: str, errors: list[str]) -> float:
    value = raw.get(field)
    if value is None or isinstance(value, bool):
        errors.append(f"{field} 缺失或不是数值。")
        return 0.0
    try:
        number = float(str(value).strip())
    except ValueError:
        errors.append(f"{field} 不是有效数值。")
        return 0.0
    if not math.isfinite(number):
        errors.append(f"{field} 不能是无穷大或 NaN。")
        return 0.0
    return number


def _volume(raw: RawRecord, errors: list[str]) -> int:
    value = raw.get("volume")
    if value is None or isinstance(value, bool):
        errors.append("volume 缺失或不是非负整数。")
        return 0
    try:
        number = float(str(value).strip())
    except ValueError:
        errors.append("volume 不是非负整数。")
        return 0
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        errors.append("volume 不是非负整数。")
        return 0
    return int(number)


def _datetime(raw: RawRecord, field: str, errors: list[str]) -> datetime | None:
    value = raw.get(field)
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError
    except ValueError:
        errors.append(f"{field} 不是有效的 ISO 8601 时间。")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{field} 必须带时区，例如 +08:00。")
        return None
    return parsed.astimezone(CHINA_TZ)


def _date(raw: RawRecord, field: str, errors: list[str]) -> date | None:
    value = raw.get(field)
    try:
        if isinstance(value, datetime):
            return value.astimezone(CHINA_TZ).date() if value.tzinfo else value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value.strip())
        raise ValueError
    except ValueError:
        errors.append(f"{field} 必须使用 YYYY-MM-DD 格式。")
        return None


def _bool(raw: RawRecord, field: str, default: bool, errors: list[str]) -> bool:
    value = raw.get(field, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    errors.append(f"{field} 必须是 true 或 false。")
    return default


def _ohlcv(raw: RawRecord, errors: list[str]) -> tuple[float, float, float, float, int]:
    open_price = _number(raw, "open", errors)
    high = _number(raw, "high", errors)
    low = _number(raw, "low", errors)
    close = _number(raw, "close", errors)
    volume = _volume(raw, errors)
    if any(value <= 0 for value in (open_price, high, low, close)):
        errors.append("open、high、low、close 必须大于 0。")
    elif high < max(open_price, close, low) or low > min(open_price, close, high):
        errors.append("OHLC 关系不成立：high/low 与开盘价或收盘价矛盾。")
    return open_price, high, low, close, volume


def normalize_minute(
    record: AdapterRecord,
    *,
    default_source: str = "unknown",
    replay_clock: datetime | None = None,
) -> CleanResult:
    raw = raw_mapping(record)
    errors: list[str] = []
    code = _code(raw, errors)
    source = _source(raw, default_source, errors)
    bar_time = _datetime(raw, "bar_time", errors)
    confirmed = _bool(raw, "confirmed", True, errors)
    open_price, high, low, close, volume = _ohlcv(raw, errors)
    adjustment = str(raw.get("adjustment", "none") or "none").strip()
    if adjustment != "none":
        errors.append("分钟行情必须使用原始未复权口径，adjustment 应为 none。")
    if bar_time is not None:
        if not is_session_minute(bar_time):
            errors.append("bar_time 不在 A 股一分钟交易时段内，或秒数不为 0。")
        if replay_clock is not None:
            if replay_clock.tzinfo is None or replay_clock.utcoffset() is None:
                errors.append("replay_clock 必须带时区。")
            elif bar_time > replay_clock.astimezone(CHINA_TZ):
                errors.append("bar_time 晚于回放时钟，属于未来数据。")
    if errors:
        return CleanResult(
            None, _issue("minute", raw, errors, time_field="bar_time")
        )
    assert bar_time is not None
    return CleanResult(
        MinuteBar(
            stock_code=code,
            bar_time=bar_time,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source=source,
            adjustment="none",
            confirmed=confirmed,
        ),
        (),
    )


def normalize_daily(record: AdapterRecord, *, default_source: str = "unknown") -> CleanResult:
    raw = raw_mapping(record)
    errors: list[str] = []
    code = _code(raw, errors)
    source = _source(raw, default_source, errors)
    trade_date = _date(raw, "trade_date", errors)
    confirmed = _bool(raw, "confirmed", True, errors)
    open_price, high, low, close, volume = _ohlcv(raw, errors)
    adjustment = str(raw.get("adjustment", "") or "").strip()
    if adjustment != "qfq":
        errors.append("日线必须明确使用前复权口径，adjustment 应为 qfq。")
    if errors:
        return CleanResult(
            None, _issue("daily_qfq", raw, errors, time_field="trade_date")
        )
    assert trade_date is not None
    return CleanResult(
        DailyBar(
            stock_code=code,
            trade_date=trade_date,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source=source,
            adjustment="qfq",
            confirmed=confirmed,
        ),
        (),
    )


def normalize_trading_day(record: AdapterRecord, *, default_source: str = "unknown") -> CleanResult:
    raw = raw_mapping(record)
    errors: list[str] = []
    trade_date = _date(raw, "trade_date", errors)
    is_open = _bool(raw, "is_open", False, errors)
    source = _source(raw, default_source, errors)
    if errors:
        return CleanResult(
            None, _issue("calendar", raw, errors, time_field="trade_date")
        )
    assert trade_date is not None
    return CleanResult(TradingDay(trade_date, is_open, source), ())


def minute_gap_issues(new_bars: list[MinuteBar], previous: MinuteBar | None) -> list[QualityIssue]:
    """检查一批新增分钟记录是否连续，不跨交易日猜测缺失。"""
    if not new_bars:
        return []
    ordered = sorted(new_bars, key=lambda item: item.bar_time)
    issues: list[QualityIssue] = []
    pairs: list[tuple[MinuteBar, MinuteBar]] = []
    if (
        previous is not None
        and previous.bar_time < ordered[0].bar_time
        and previous.bar_time.date() == ordered[0].bar_time.date()
    ):
        pairs.append((previous, ordered[0]))
    pairs.extend(zip(ordered, ordered[1:]))
    for earlier, later in pairs:
        if earlier.bar_time.date() != later.bar_time.date():
            continue
        expected = previous_session_minute(later.bar_time)
        if expected is not None and earlier.bar_time != expected:
            issues.append(
                QualityIssue(
                    data_kind="minute",
                    issue_type="minute_gap",
                    severity="error",
                    message=(
                        f"分钟数据存在缺口：{earlier.bar_time.isoformat()} 之后"
                        f"直接跳到 {later.bar_time.isoformat()}。"
                    ),
                    raw_json=raw_to_json(later),
                    stock_code=later.stock_code,
                    data_time=later.bar_time.isoformat(),
                )
            )
    return issues
