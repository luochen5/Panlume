"""校验适配器返回范围，防止合法但答非所问的数据污染本地行情。

这些检查发生在清洗入库之前。格式错误的单条记录仍交给清洗层记录；
代码、时间或日期已经明确超出本次请求范围时，整次读取按失败处理。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from .base import AdapterRecord
from .cleaner import raw_mapping
from .schedule import CHINA_TZ


class AdapterResponseError(ValueError):
    """适配器返回了不属于本次只读请求的数据。"""


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须带时区。")
    return value.astimezone(CHINA_TZ)


def _parsed_datetime(value: object) -> datetime | None:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CHINA_TZ)


def _parsed_date(value: object) -> date | None:
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value.strip())
    except ValueError:
        pass
    return None


def ensure_minute_scope(
    records: Iterable[AdapterRecord],
    stock_codes: Iterable[str],
    start: datetime,
    end: datetime,
) -> list[AdapterRecord]:
    """只接受请求代码和闭区间内的分钟；无法解析的字段留给清洗层。"""
    codes = frozenset(stock_codes)
    start = _aware(start, "start")
    end = _aware(end, "end")
    if not codes:
        raise ValueError("分钟请求至少需要一个股票代码。")
    if start > end:
        raise ValueError("分钟请求的起始时间不能晚于结束时间。")
    items = list(records)
    for index, record in enumerate(items):
        raw = raw_mapping(record)
        code = raw.get("stock_code")
        if isinstance(code, str) and code.strip() and code.strip() not in codes:
            raise AdapterResponseError(
                f"第 {index} 条分钟记录代码 {code.strip()} 不在本次请求范围内。"
            )
        bar_time = _parsed_datetime(raw.get("bar_time"))
        if bar_time is not None and not start <= bar_time <= end:
            raise AdapterResponseError(
                f"第 {index} 条分钟记录时间 {bar_time.isoformat()} 超出请求闭区间 "
                f"{start.isoformat()} 至 {end.isoformat()}。"
            )
    return items


def ensure_daily_scope(
    records: Iterable[AdapterRecord],
    stock_codes: Iterable[str],
    start: date,
    end: date,
) -> list[AdapterRecord]:
    codes = frozenset(stock_codes)
    if not codes:
        raise ValueError("日线请求至少需要一个股票代码。")
    if start > end:
        raise ValueError("日线请求的起始日期不能晚于结束日期。")
    items = list(records)
    for index, record in enumerate(items):
        raw = raw_mapping(record)
        code = raw.get("stock_code")
        if isinstance(code, str) and code.strip() and code.strip() not in codes:
            raise AdapterResponseError(
                f"第 {index} 条日线记录代码 {code.strip()} 不在本次请求范围内。"
            )
        trade_date = _parsed_date(raw.get("trade_date"))
        if trade_date is not None and not start <= trade_date <= end:
            raise AdapterResponseError(
                f"第 {index} 条日线日期 {trade_date.isoformat()} 超出请求闭区间 "
                f"{start.isoformat()} 至 {end.isoformat()}。"
            )
    return items


def ensure_calendar_scope(
    records: Iterable[AdapterRecord], start: date, end: date
) -> list[AdapterRecord]:
    if start > end:
        raise ValueError("日历请求的起始日期不能晚于结束日期。")
    items = list(records)
    for index, record in enumerate(items):
        raw = raw_mapping(record)
        trade_date = _parsed_date(raw.get("trade_date"))
        if trade_date is not None and not start <= trade_date <= end:
            raise AdapterResponseError(
                f"第 {index} 条日历日期 {trade_date.isoformat()} 超出请求闭区间 "
                f"{start.isoformat()} 至 {end.isoformat()}。"
            )
    return items
