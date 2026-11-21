from datetime import date

from pandeng.data.schedule import CHINA_TZ, session_minutes
from pandeng.data.simulator import demo_time_range, make_simulator


def test_session_has_240_minutes_and_connects_lunch_break() -> None:
    points = session_minutes(date(2026, 1, 5))
    assert len(points) == 240
    assert points[0].time().isoformat() == "09:31:00"
    assert points[119].time().isoformat() == "11:30:00"
    assert points[120].time().isoformat() == "13:01:00"
    assert points[-1].time().isoformat() == "15:00:00"
    assert all(point.tzinfo == CHINA_TZ for point in points)


def test_simulator_is_deterministic_and_covers_three_data_kinds() -> None:
    day = date(2026, 1, 5)
    start, end = demo_time_range(day, 3)
    first = make_simulator(["600000"], day, daily_days=5)
    second = make_simulator(["600000"], day, daily_days=5)

    first_minutes = list(first.fetch_minute_bars(["600000"], start, end))
    second_minutes = list(second.fetch_minute_bars(["600000"], start, end))
    daily = list(first.fetch_daily_qfq(["600000"], first.dates[0], day))
    calendar = list(first.fetch_trading_calendar(first.dates[0], day))

    assert first_minutes == second_minutes
    assert len(first_minutes) == 3
    assert all(item.adjustment == "none" for item in first_minutes)
    assert len(daily) == 5
    assert all(item.adjustment == "qfq" and item.confirmed for item in daily)
    assert len(calendar) >= 5
