from datetime import datetime

from pandeng.data.cleaner import (
    normalize_daily,
    normalize_minute,
    normalize_trading_day,
)
from pandeng.data.schedule import CHINA_TZ


def minute_record(**changes):
    record = {
        "stock_code": "600000",
        "bar_time": "2026-01-05T09:31:00+08:00",
        "open": "10.00",
        "high": "10.20",
        "low": "9.90",
        "close": "10.10",
        "volume": "1000",
        "source": "test",
        "adjustment": "none",
        "confirmed": "true",
    }
    record.update(changes)
    return record


def daily_record(**changes):
    record = {
        "stock_code": "600000",
        "trade_date": "2026-01-05",
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "volume": 10000,
        "source": "test",
        "adjustment": "qfq",
        "confirmed": True,
    }
    record.update(changes)
    return record


def test_valid_minute_is_normalized_to_china_time() -> None:
    result = normalize_minute(minute_record())
    assert result.issues == ()
    assert result.record is not None
    assert result.record.bar_time.isoformat() == "2026-01-05T09:31:00+08:00"
    assert result.record.volume == 1000


def test_naive_minute_time_is_rejected() -> None:
    result = normalize_minute(minute_record(bar_time="2026-01-05 09:31:00"))
    assert result.record is None
    assert any("必须带时区" in issue.message for issue in result.issues)


def test_future_data_is_rejected_against_replay_clock() -> None:
    clock = datetime(2026, 1, 5, 9, 30, tzinfo=CHINA_TZ)
    result = normalize_minute(minute_record(), replay_clock=clock)
    assert result.record is None
    assert any("未来数据" in issue.message for issue in result.issues)


def test_invalid_ohlc_and_missing_volume_are_rejected() -> None:
    result = normalize_minute(minute_record(high="9.0", volume=""))
    assert result.record is None
    assert len(result.issues) >= 2


def test_adjusted_minute_price_is_rejected() -> None:
    result = normalize_minute(minute_record(adjustment="qfq"))
    assert result.record is None
    assert any("原始未复权" in issue.message for issue in result.issues)


def test_daily_requires_qfq_and_keeps_confirmation_state() -> None:
    wrong = normalize_daily(daily_record(adjustment="none"))
    assert wrong.record is None

    unconfirmed = normalize_daily(daily_record(confirmed=False))
    assert unconfirmed.record is not None
    assert unconfirmed.record.confirmed is False


def test_calendar_requires_explicit_boolean() -> None:
    valid = normalize_trading_day(
        {"trade_date": "2026-01-05", "is_open": "true", "source": "test"}
    )
    invalid = normalize_trading_day(
        {"trade_date": "2026-01-05", "is_open": "maybe", "source": "test"}
    )
    assert valid.record is not None and valid.record.is_open is True
    assert invalid.record is None
