from datetime import date, datetime
from pathlib import Path

import pytest

from pandeng.data.audit import audit_market_data
from pandeng.data.schedule import CHINA_TZ
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes
from test_cleaner import daily_record, minute_record


DAY = date(2026, 1, 12)
CODES = ("600000", "000001")


def moment(minute: int) -> datetime:
    return datetime(2026, 1, 12, 9, minute, tzinfo=CHINA_TZ)


def minutes(code: str, points: list[int], **changes: object) -> list[dict]:
    return [
        minute_record(
            stock_code=code,
            bar_time=f"2026-01-12T09:{point:02d}:00+08:00",
            confirmed=True,
            source="audit_fixture",
            **changes,
        )
        for point in points
    ]


def make_database(tmp_path: Path, *, calendar: bool = True) -> Database:
    db = Database(tmp_path / "audit.db")
    db.initialize()
    if calendar:
        ingest_calendar(db, [{
            "trade_date": DAY.isoformat(), "is_open": True, "source": "audit_fixture"
        }], adapter="audit_fixture")
    for code in CODES:
        ingest_minutes(db, minutes(code, [31, 32, 33]), adapter="audit_fixture")
        ingest_daily(db, [daily_record(
            stock_code=code, trade_date=DAY.isoformat(), confirmed=True
        )], adapter="audit_fixture")
    return db


def test_complete_audit_separates_daily_and_reports_sources_without_writes(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    with db.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0]

    report = audit_market_data(db, CODES, DAY, moment(33))

    assert report.status == "ok" and report.calendar_status == "open"
    assert report.complete_count == 2 and report.unknown_count == 0
    for item in report.codes:
        assert item.status == "ok"
        assert (item.expected_minutes, item.saved_minutes, item.confirmed_minutes) == (3, 3, 3)
        assert item.missing_times == () and item.quality_error_count == 0
        assert item.price_line_available and item.target_confirmed is True
        assert item.sources == ("audit_fixture",)
        assert item.daily_status == "confirmed"
        assert item.checkpoint_status is None
    with db.connect() as connection:
        after = connection.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0]
    assert after == before


def test_history_gap_is_price_only_when_target_minute_is_valid(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    # 另建一只只有 09:31 和 09:33 的代码，关闭入库缺口检查以单独测试只读审计。
    other = Database(tmp_path / "gap.db")
    other.initialize()
    ingest_calendar(other, [{
        "trade_date": DAY.isoformat(), "is_open": True, "source": "audit_fixture"
    }], adapter="audit_fixture")
    ingest_minutes(
        other, minutes("600000", [31, 33]), adapter="audit_fixture", check_gaps=False
    )

    item = audit_market_data(other, ["600000"], DAY, moment(33)).codes[0]

    assert item.status == "price_only"
    assert item.missing_times == ("2026-01-12T09:32:00+08:00",)
    assert item.price_line_available and item.target_confirmed is True
    assert item.saved_minutes == 2 and item.expected_minutes == 3


def test_missing_target_minute_makes_price_line_unknown(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM minute_bars WHERE stock_code = '600000' AND bar_time = ?",
            (moment(33).isoformat(),),
        )

    item = audit_market_data(db, ["600000"], DAY, moment(33)).codes[0]

    assert item.status == "unknown" and not item.price_line_available
    assert item.latest_minute_time == moment(32).isoformat()
    assert item.target_confirmed is None
    assert any("目标分钟缺失" in reason for reason in item.reasons)


def test_unconfirmed_target_is_unknown_but_unconfirmed_history_preserves_price_line(
    tmp_path: Path,
) -> None:
    target_db = make_database(tmp_path)
    with target_db.connect() as connection:
        connection.execute(
            "UPDATE minute_bars SET confirmed = 0 "
            "WHERE stock_code = '600000' AND bar_time = ?",
            (moment(33).isoformat(),),
        )
    target = audit_market_data(target_db, ["600000"], DAY, moment(33)).codes[0]
    assert target.status == "unknown" and not target.price_line_available
    assert target.unconfirmed_minutes == 1

    history_db = make_database(tmp_path / "history")
    with history_db.connect() as connection:
        connection.execute(
            "UPDATE minute_bars SET confirmed = 0 "
            "WHERE stock_code = '600000' AND bar_time = ?",
            (moment(32).isoformat(),),
        )
    history = audit_market_data(history_db, ["600000"], DAY, moment(33)).codes[0]
    assert history.status == "price_only" and history.price_line_available
    assert history.unconfirmed_minutes == 1


def test_target_conflict_is_unknown_even_if_original_row_remains(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    conflict = minute_record(
        stock_code="600000", bar_time=moment(33).isoformat(), close="99.99"
    )
    ingest_minutes(db, [conflict], adapter="audit_fixture")

    item = audit_market_data(db, ["600000"], DAY, moment(33)).codes[0]

    assert item.status == "unknown" and not item.price_line_available
    assert item.target_confirmed is True  # 原记录仍在，但冲突使结论不可用。
    assert item.quality_error_count == 1
    assert any("目标分钟存在质量冲突" in reason for reason in item.reasons)


def test_daily_unconfirmed_or_conflicting_does_not_relabel_complete_minutes(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    with db.connect() as connection:
        connection.execute(
            "UPDATE daily_qfq_bars SET confirmed = 0 WHERE stock_code = '600000'"
        )
    unconfirmed = audit_market_data(db, ["600000"], DAY, moment(33)).codes[0]
    assert unconfirmed.status == "ok" and unconfirmed.daily_status == "unconfirmed"

    conflict = daily_record(
        stock_code="600000", trade_date=DAY.isoformat(), confirmed=True, close=88.8
    )
    ingest_daily(db, [conflict], adapter="audit_fixture")
    conflicting = audit_market_data(db, ["600000"], DAY, moment(33)).codes[0]
    assert conflicting.status == "ok" and conflicting.daily_status == "unknown"


def test_calendar_missing_or_conflicting_is_explicitly_unknown(tmp_path: Path) -> None:
    missing_db = make_database(tmp_path, calendar=False)
    missing = audit_market_data(missing_db, ["600000"], DAY, moment(33))
    assert missing.calendar_status == "unknown"
    assert missing.codes[0].status == "price_only"
    assert missing.codes[0].price_line_available

    conflict_db = make_database(tmp_path / "calendar-conflict")
    ingest_calendar(conflict_db, [{
        "trade_date": DAY.isoformat(), "is_open": False, "source": "different"
    }], adapter="different")
    conflict = audit_market_data(conflict_db, ["600000"], DAY, moment(33))
    assert conflict.calendar_status == "unknown"
    assert any("缺失或存在冲突" in reason for reason in conflict.codes[0].reasons)


def test_closed_calendar_does_not_turn_saved_minutes_into_price_line(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    with db.connect() as connection:
        connection.execute("UPDATE trading_calendar SET is_open = 0")
    report = audit_market_data(db, ["600000"], DAY, moment(33))
    assert report.calendar_status == "closed"
    assert report.codes[0].status == "unknown"
    assert not report.codes[0].price_line_available


def test_quality_error_after_as_of_time_does_not_pollute_earlier_audit(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    ingest_minutes(db, minutes("600000", [34]), adapter="audit_fixture")
    ingest_minutes(db, [minute_record(
        stock_code="600000", bar_time=moment(34).isoformat(), close="77.7"
    )], adapter="audit_fixture")

    item = audit_market_data(db, ["600000"], DAY, moment(33)).codes[0]
    assert item.status == "ok" and item.quality_error_count == 0


@pytest.mark.parametrize("value", [
    datetime(2026, 1, 12, 9, 33),
    datetime(2026, 1, 13, 9, 33, tzinfo=CHINA_TZ),
    datetime(2026, 1, 12, 12, 0, tzinfo=CHINA_TZ),
    datetime(2026, 1, 12, 9, 33, 30, tzinfo=CHINA_TZ),
])
def test_audit_rejects_ambiguous_as_of_time(tmp_path: Path, value: datetime) -> None:
    db = make_database(tmp_path)
    with pytest.raises(ValueError):
        audit_market_data(db, CODES, DAY, value)


def test_audit_requires_at_least_one_code(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    with pytest.raises(ValueError, match="至少需要"):
        audit_market_data(db, [], DAY, moment(33))
