"""把电脑时间映射到交易阶段和本轮应补齐的一分钟终点。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from pandeng.data.schedule import (
    AFTERNOON_END,
    AFTERNOON_START,
    CHINA_TZ,
    MORNING_END,
    MORNING_START,
)


def local_minute(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("调度时间必须带时区。")
    return value.astimezone(CHINA_TZ).replace(second=0, microsecond=0)


def session_target(value: datetime) -> datetime | None:
    """返回当前已经到达的交易分钟；午休和开盘前返回 None。"""
    local = local_minute(value)
    clock = local.time()
    if MORNING_START <= clock <= MORNING_END or AFTERNOON_START <= clock <= AFTERNOON_END:
        return local
    return None


def day_boundary(session_date: date, boundary: time) -> datetime:
    return datetime.combine(session_date, boundary, CHINA_TZ)


def next_poll_time(now: datetime, interval_seconds: int) -> datetime:
    """计算下一次检查；收盘边界由调度器另行强制检查。"""
    return now.astimezone(CHINA_TZ) + timedelta(seconds=interval_seconds)


def seconds_until(later: datetime, now: datetime) -> float:
    return max(0.0, (later.astimezone(CHINA_TZ) - now.astimezone(CHINA_TZ)).total_seconds())
