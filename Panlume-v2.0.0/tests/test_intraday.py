from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

from pandeng.data.base import MinuteBar, TradingDay
from pandeng.data.schedule import CHINA_TZ, recent_weekdays, session_minutes
from pandeng.indicators.intraday import calculate_intraday
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_minutes


CODE = "600000"
DAY = date(2026, 1, 5)


def bar(code: str, index: int, close: float, volume: int = 100) -> MinuteBar:
    point = session_minutes(DAY)[index]
    return MinuteBar(
        code, point, close, close + 0.1, max(0.01, close - 0.1), close,
        volume, "test", "none", True,
    )


def initialized(tmp_path: Path) -> Database:
    database = Database(tmp_path / "indicator.db")
    database.initialize()
    return database


def baseline_calendar(open_days: list[date], *, omit: date | None = None) -> list[TradingDay]:
    """补齐最早基线日到当前日前一天的自然日，周末明确记为休市。"""
    open_set = set(open_days)
    cursor = min(open_days)
    result = []
    while cursor < DAY:
        if cursor != omit:
            result.append(TradingDay(cursor, cursor in open_set, "test"))
        cursor += timedelta(days=1)
    return result


def test_intraday_formulas_and_source_trace(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    points = session_minutes(DAY)
    current = [bar(CODE, index, 10.0 + index * 0.1, volume=100 + index) for index in range(21)]
    history_days = recent_weekdays(DAY, 21)[:-1]
    historical = [
        MinuteBar(
            CODE, point.replace(day=day.day, month=day.month, year=day.year),
            8, 8.2, 7.8, 8, 200, "test", "none", True,
        )
        for day in history_days
        for point in [points[20]]
    ]
    sector = [
        MinuteBar(
            "399001", point, 21 if index == 20 else 20,
            (21 if index == 20 else 20) + 0.1,
            (21 if index == 20 else 20) - 0.1,
            21 if index == 20 else 20,
            100, "sector-test",
        )
        for index, point in enumerate(points[:21])
    ]
    assert ingest_minutes(database, [*historical, *current, *sector], adapter="test", check_gaps=False).status == "ok"
    ingest_calendar(database, baseline_calendar(history_days), adapter="test")

    result = calculate_intraday(
        database, CODE, points[20], sector_code="399001", breakout_lookback=20
    )

    assert result.status == "ok"
    values = result.values
    assert values["return_1m_pct"] == math.copysign(abs((12 / 11.9 - 1) * 100), 1)
    assert values["return_5m_pct"] == (12 / 11.5 - 1) * 100
    assert values["ma5"] == sum([11.6, 11.7, 11.8, 11.9, 12.0]) / 5
    assert values["ma20_minute"] == sum(10.0 + index * 0.1 for index in range(1, 21)) / 20
    assert values["volume_ratio"] == 120 / 200
    assert values["prior_breakout_high"] == 12.0  # 前一根收盘 11.9 + high 0.1，不含当前 12.1
    expected_vwap = sum(
        (((10.0 + i * 0.1 + 0.1) + (10.0 + i * 0.1 - 0.1) + (10.0 + i * 0.1)) / 3)
        * (100 + i)
        for i in range(21)
    ) / sum(100 + i for i in range(21))
    assert values["vwap_estimate"] == expected_vwap
    stock_return = 12 / 10 - 1
    sector_return = 21 / 20 - 1
    assert values["relative_sector_pct"] == (stock_return - sector_return) * 100
    with database.connect() as connection:
        sector_ids = {
            row[0] for row in connection.execute(
                "SELECT id FROM minute_bars WHERE stock_code = '399001'"
            )
        }
    assert sector_ids <= set(result.source_bar_ids)
    assert len(result.source_bar_ids) == 62
    assert result.latest_minute_bar_id is not None


def test_warmup_and_missing_sector_are_field_unknowns_not_snapshot_failure(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    ingest_minutes(database, [bar(CODE, 0, 10)], adapter="test")

    result = calculate_intraday(database, CODE, session_minutes(DAY)[0])

    assert result.status == "ok" and result.values["close"] == 10
    assert result.values["return_1m_pct"] is None
    assert result.values["volume_ratio"] is None
    assert result.values["relative_sector_pct"] is None
    assert any("sector_code" in reason for reason in result.unknown_reasons)


def test_history_gap_preserves_price_line_but_makes_context_unknown(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    ingest_minutes(database, [bar(CODE, 0, 10), bar(CODE, 2, 11)], adapter="test", check_gaps=False)

    result = calculate_intraday(database, CODE, session_minutes(DAY)[2])

    assert result.status == "ok"
    assert "不连续" in result.unknown_reasons[0]
    assert result.values["close"] == 11
    assert result.values["volume"] == 100
    assert result.values["vwap_estimate"] is None
    assert result.values["return_1m_pct"] is None
    assert result.values["day_high"] is None
    assert result.source_bar_ids == (result.latest_minute_bar_id,)


def test_missing_or_unconfirmed_target_minute_makes_price_line_unknown(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    ingest_minutes(
        database,
        [bar(CODE, 0, 10), bar(CODE, 2, 11)],
        adapter="test",
        check_gaps=False,
    )

    missing = calculate_intraday(database, CODE, session_minutes(DAY)[1])
    assert missing.status == "unknown" and missing.values["close"] is None

    with database.connect() as connection:
        connection.execute(
            "UPDATE minute_bars SET confirmed = 0 WHERE bar_time = ?",
            (session_minutes(DAY)[2].isoformat(),),
        )
    unconfirmed = calculate_intraday(database, CODE, session_minutes(DAY)[2])
    assert unconfirmed.status == "unknown" and unconfirmed.values["close"] is None


def test_history_baseline_requires_all_twenty_confirmed_days(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    days = recent_weekdays(DAY, 21)[:-1]
    point = session_minutes(DAY)[20]
    current = [bar(CODE, index, 10) for index in range(21)]
    historical = [
        MinuteBar(CODE, point.replace(year=day.year, month=day.month, day=day.day),
                  8, 8.1, 7.9, 8, 200, "test", confirmed=(day != days[0]))
        for day in days
    ]
    ingest_minutes(database, [*historical, *current], adapter="test")
    ingest_calendar(database, baseline_calendar(days), adapter="test")

    result = calculate_intraday(database, CODE, point)

    assert result.status == "ok" and result.values["volume_ratio"] is None
    assert any("成交量不完整" in reason for reason in result.unknown_reasons)


def test_missing_calendar_day_cannot_be_replaced_by_older_open_day(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    days = recent_weekdays(DAY, 22)[:-1]  # 21 个历史工作日，故意制造一个日历空洞。
    missing_calendar_day = days[-1]
    point = session_minutes(DAY)[20]
    current = [bar(CODE, index, 10) for index in range(21)]
    historical = [
        MinuteBar(
            CODE, point.replace(year=day.year, month=day.month, day=day.day),
            8, 8.1, 7.9, 8, 200, "test", confirmed=True,
        )
        for day in days
    ]
    ingest_minutes(database, [*historical, *current], adapter="test")
    ingest_calendar(
        database,
        baseline_calendar(days, omit=missing_calendar_day),
        adapter="test",
    )

    result = calculate_intraday(database, CODE, point)

    assert result.status == "ok" and result.values["volume_ratio"] is None
    assert any("不能用更早日历替代缺失日期" in reason for reason in result.unknown_reasons)
    assert not any("成交量不完整" in reason for reason in result.unknown_reasons)


def test_sector_gap_makes_only_relative_performance_unknown(tmp_path: Path) -> None:
    database = initialized(tmp_path)
    points = session_minutes(DAY)
    current = [bar(CODE, index, 10 + index * 0.1) for index in range(21)]
    sector = [
        MinuteBar("399001", points[index], 20, 20.1, 19.9, 20, 100, "sector-test")
        for index in range(21) if index != 10
    ]
    ingest_minutes(database, [*current, *sector], adapter="test", check_gaps=False)

    result = calculate_intraday(database, CODE, points[20], sector_code="399001")

    assert result.status == "ok" and result.values["close"] == 12
    assert result.values["relative_sector_pct"] is None
    assert any("板块当日分钟序列不连续" in reason for reason in result.unknown_reasons)
