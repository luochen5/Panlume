from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pandeng.data.base import DailyBar
from pandeng.data.schedule import recent_weekdays
from pandeng.indicators.daily import analyze_daily
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_daily


CODE = "600000"
DAY = date(2026, 2, 2)


def make_daily(days: list[date], closes: list[float], *, confirmed_last: bool = True) -> list[DailyBar]:
    return [
        DailyBar(
            CODE, day, close, close + 0.2, close - 0.2, close, 10000, "test", "qfq",
            confirmed=(confirmed_last or index < len(days) - 1),
        )
        for index, (day, close) in enumerate(zip(days, closes))
    ]


def database_at(tmp_path: Path, name: str = "daily.db") -> Database:
    database = Database(tmp_path / name)
    database.initialize()
    return database


def test_ma20_turn_days_and_formal_signal_need_both_conditions(tmp_path: Path) -> None:
    database = database_at(tmp_path)
    days = recent_weekdays(DAY, 25)
    closes = [10.0] * 23 + [11.0, 9.0]
    ingest_daily(database, make_daily(days, closes), adapter="test")

    results = analyze_daily(database, CODE, DAY, analysis_mode="replay")

    first_ma = results[19]
    assert first_ma.ma20 == 10 and first_ma.turn_days is None
    assert first_ma.formal_signal is None and first_ma.status == "unknown"
    assert [results[index].turn_days for index in (20, 21, 22)] == [1, 2, 3]
    assert results[22].formal_signal is False  # 收盘等于 MA20，严格高于条件不成立
    assert results[23].ma20 == pytest.approx(10.05)
    assert results[23].turn_days == 4 and results[23].formal_signal is True
    assert results[24].ma20 == pytest.approx(10.0)
    assert results[24].turn_days == 0 and results[24].formal_signal is False
    assert all(item.is_backfill for item in results)
    assert all(item.analysis_mode == "replay" for item in results)


def test_ma20_decline_resets_then_non_decline_increments_from_one(tmp_path: Path) -> None:
    database = database_at(tmp_path)
    days = recent_weekdays(DAY, 24)
    closes = [10.0] * 20 + [9.0, 10.0, 10.0, 10.0]
    ingest_daily(database, make_daily(days, closes), adapter="test")

    results = analyze_daily(database, CODE, DAY)

    assert results[19].turn_days is None
    assert results[20].turn_days == 0
    assert results[21].turn_days == 1
    assert results[22].turn_days == 2
    assert results[23].turn_days == 3


def test_unconfirmed_daily_never_generates_formal_signal(tmp_path: Path) -> None:
    database = database_at(tmp_path)
    days = recent_weekdays(DAY, 23)
    ingest_daily(database, make_daily(days, [10.0] * 23, confirmed_last=False), adapter="test")

    results = analyze_daily(database, CODE, DAY, analysis_mode="live")
    target = results[-1]

    assert target.status == "unknown" and target.formal_signal is None
    assert target.ma20 is None and "未确认" in target.reason
    assert target.is_backfill is False


def test_insufficient_daily_history_is_unknown(tmp_path: Path) -> None:
    database = database_at(tmp_path)
    days = recent_weekdays(DAY, 19)
    ingest_daily(database, make_daily(days, [10.0] * 19), adapter="test")

    target = analyze_daily(database, CODE, DAY)[-1]

    assert target.status == "unknown" and target.ma20 is None
    assert target.formal_signal is None and "需要 20 根" in target.reason
