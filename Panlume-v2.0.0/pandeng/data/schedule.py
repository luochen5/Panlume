"""A 股交易分钟工具；这里只处理时间格式，不判断真实节假日。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


# 使用固定 UTC+8，避免 Windows 缺少系统时区数据库时无法运行。
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MORNING_START = time(9, 31)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 1)
AFTERNOON_END = time(15, 0)


def session_minutes(session_date: date) -> list[datetime]:
    """返回一个交易日正常应有的 240 个一分钟时间点。"""
    morning = [
        datetime.combine(session_date, MORNING_START, CHINA_TZ) + timedelta(minutes=index)
        for index in range(120)
    ]
    afternoon = [
        datetime.combine(session_date, AFTERNOON_START, CHINA_TZ)
        + timedelta(minutes=index)
        for index in range(120)
    ]
    return morning + afternoon


def is_session_minute(value: datetime) -> bool:
    local = value.astimezone(CHINA_TZ)
    clock = local.time()
    return (
        local.second == 0
        and local.microsecond == 0
        and (
            MORNING_START <= clock <= MORNING_END
            or AFTERNOON_START <= clock <= AFTERNOON_END
        )
    )


def previous_session_minute(value: datetime) -> datetime | None:
    """返回同一交易时段内的上一分钟；跨午休时连接 11:30 与 13:01。"""
    local = value.astimezone(CHINA_TZ)
    if local.time() == AFTERNOON_START:
        return datetime.combine(local.date(), MORNING_END, CHINA_TZ)
    previous = local - timedelta(minutes=1)
    return previous if is_session_minute(previous) else None


def next_session_minute(value: datetime) -> datetime | None:
    """返回下一交易分钟；11:30 后连接 13:01，15:00 后返回 None。"""
    local = value.astimezone(CHINA_TZ)
    if local.time() == MORNING_END:
        return datetime.combine(local.date(), AFTERNOON_START, CHINA_TZ)
    following = local + timedelta(minutes=1)
    return following if is_session_minute(following) else None


def recent_weekdays(end: date, count: int) -> list[date]:
    """生成模拟用工作日序列；真实节假日必须由交易日历数据决定。"""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))
