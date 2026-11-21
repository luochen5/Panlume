from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import RulesConfig
from pandeng.data.base import MinuteBar, TradingDay
from pandeng.data.schedule import recent_weekdays, session_minutes
from pandeng.holdings.positions import CONFIRM_ADD, CONFIRM_ARCHIVE, add_position_lot, archive_position_lot
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_minutes


CODE = "600000"
DAY = date(2026, 1, 5)
POINTS = session_minutes(DAY)


def flat_bar(index: int, close: float) -> MinuteBar:
    return MinuteBar(CODE, POINTS[index], close, close, close, close, 100, "test")


def vwap_bar(index: int) -> MinuteBar:
    return MinuteBar(CODE, POINTS[index], 10, 10, 9, 9, 100, "test")


def setup_service(tmp_path: Path, bars: list[MinuteBar], name: str = "rules.db") -> tuple[Database, AnalysisService]:
    database = Database(tmp_path / name)
    database.initialize()
    ingest_minutes(database, bars, adapter="test", check_gaps=False)
    rules = RulesConfig(
        rapid_move_pct=20.0, normal_cooldown_seconds=900,
        high_priority_repeat_seconds=900,
    )
    return database, AnalysisService(database, rules=rules)


def add_stop(database: Database, stop: float = 10.0) -> int:
    return add_position_lot(
        database, stock_code=CODE, quantity=100, buy_date=DAY.isoformat(),
        stop_loss_price=stop, confirmation=CONFIRM_ADD,
    )


def analyze(service: AnalysisService, index: int, mode: str = "live"):
    return service.analyze_code(CODE, POINTS[index], analysis_mode=mode)


def test_fixed_stop_first_repeat_clear_and_reactivate(tmp_path: Path) -> None:
    bars = [flat_bar(index, 9) for index in range(17)]
    bars.extend([flat_bar(17, 11), flat_bar(18, 9)])
    database, service = setup_service(tmp_path, bars)
    lot_id = add_stop(database)

    first = analyze(service, 0)
    assert len(first.notices) == 1
    assert first.notices[0].notification_kind == "initial"
    assert first.notices[0].position_lot_id == lot_id
    assert "T+1 可卖状态 unknown" in first.notices[0].message
    assert "不执行交易" in first.notices[0].message

    assert analyze(service, 1).notices == ()
    repeated = analyze(service, 16)
    assert len(repeated.notices) == 1
    assert repeated.notices[0].notification_kind == "repeat"

    analyze(service, 17)
    with database.connect() as connection:
        cleared = connection.execute(
            "SELECT status FROM rule_events WHERE event_type = 'fixed_stop' ORDER BY id"
        ).fetchall()
    assert [row[0] for row in cleared] == ["cleared"]

    reactivated = analyze(service, 18)
    stop_notices = [notice for notice in reactivated.notices if notice.event_type == "fixed_stop"]
    assert len(stop_notices) == 1
    assert stop_notices[0].notification_kind == "initial"
    with database.connect() as connection:
        events = list(connection.execute(
            "SELECT status, reminder_count FROM rule_events WHERE event_type = 'fixed_stop' ORDER BY id"
        ))
    assert [(row["status"], row["reminder_count"]) for row in events] == [
        ("cleared", 2), ("active", 1),
    ]


def test_stop_touch_boundary_clear_reactivate_and_idempotent_run(tmp_path: Path) -> None:
    bars = [
        flat_bar(0, 11),  # 高于：未触发
        flat_bar(1, 10),  # 等于：触发
        flat_bar(2, 9),   # 低于：持续触发，冷却期内不重复提醒
        flat_bar(3, 11),  # 回到上方：解除
        flat_bar(4, 10),  # 再次等于：新事件立即提醒
    ]
    database, service = setup_service(tmp_path, bars)
    add_stop(database)

    def stops(result):
        return [notice for notice in result.notices if notice.event_type == "fixed_stop"]

    assert stops(analyze(service, 0)) == []
    equal = analyze(service, 1)
    assert len(stops(equal)) == 1
    assert "小于或等于止损价" in stops(equal)[0].message
    repeated = analyze(service, 1)
    assert repeated.skipped is True and repeated.notices == ()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2

    assert stops(analyze(service, 2)) == []
    assert stops(analyze(service, 3)) == []
    reactivated = stops(analyze(service, 4))
    assert len(reactivated) == 1 and reactivated[0].notification_kind == "initial"
    with database.connect() as connection:
        events = list(connection.execute(
            "SELECT status FROM rule_events WHERE event_type = 'fixed_stop' ORDER BY id"
        ))
    assert [row["status"] for row in events] == ["cleared", "active"]


def test_history_gap_does_not_block_valid_current_minute_stop(tmp_path: Path) -> None:
    database, service = setup_service(
        tmp_path,
        [flat_bar(0, 11), flat_bar(2, 10)],
        name="gap-stop.db",
    )
    add_stop(database)

    result = analyze(service, 2)

    stops = [notice for notice in result.notices if notice.event_type == "fixed_stop"]
    assert result.status == "ok" and len(stops) == 1
    assert stops[0].event_check_id is not None
    with database.connect() as connection:
        snapshot = connection.execute(
            "SELECT * FROM indicator_snapshots WHERE id = ?", (result.snapshot_id,)
        ).fetchone()
    values = json.loads(snapshot["values_json"])
    reasons = json.loads(snapshot["unknown_reasons_json"])
    assert values["close"] == 10 and values["vwap_estimate"] is None
    assert values["return_1m_pct"] is None
    assert any("历史分钟序列不完整" in reason for reason in reasons)


def test_recovery_is_backfill_then_first_live_alert_is_immediate(tmp_path: Path) -> None:
    database, service = setup_service(tmp_path, [flat_bar(0, 9), flat_bar(1, 9)])
    add_stop(database)

    recovery = analyze(service, 0, "offline_recovery")
    assert recovery.notices == ()
    with database.connect() as connection:
        check = connection.execute("SELECT * FROM rule_event_checks").fetchone()
        event = connection.execute("SELECT * FROM rule_events").fetchone()
    assert check["notification_kind"] == "recovery" and check["notified"] == 0
    assert event["reminder_count"] == 0 and event["last_reminded_at"] is None

    live = analyze(service, 1, "live")
    assert len(live.notices) == 1 and live.notices[0].notification_kind == "initial"


def test_normal_event_has_independent_cooldown(tmp_path: Path) -> None:
    bars = [vwap_bar(index) for index in range(16)]
    database, service = setup_service(tmp_path, bars)

    first = analyze(service, 0)
    assert [notice.event_type for notice in first.notices] == ["below_vwap"]
    assert analyze(service, 1).notices == ()
    repeated = analyze(service, 15)
    assert [notice.event_type for notice in repeated.notices] == ["below_vwap"]
    assert repeated.notices[0].notification_kind == "repeat"

    with database.connect() as connection:
        event = connection.execute("SELECT * FROM rule_events WHERE event_type = 'below_vwap'").fetchone()
        checks = connection.execute(
            "SELECT COUNT(*) FROM rule_event_checks WHERE event_id = ?", (event["id"],)
        ).fetchone()[0]
    assert event["reminder_count"] == 2 and checks == 3


def test_unknown_snapshot_does_not_clear_active_stop(tmp_path: Path) -> None:
    database, service = setup_service(tmp_path, [flat_bar(0, 9), flat_bar(2, 9)])
    add_stop(database)
    assert len(analyze(service, 0).notices) == 1

    unknown = analyze(service, 1)

    assert unknown.status == "unknown" and unknown.notices == ()
    with database.connect() as connection:
        event = connection.execute("SELECT * FROM rule_events WHERE event_type = 'fixed_stop'").fetchone()
        latest = connection.execute(
            "SELECT * FROM rule_event_checks WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event["id"],),
        ).fetchone()
    assert event["status"] == "active" and latest["is_active"] is None
    assert latest["notified"] == 0


def test_archiving_lot_closes_old_stop_without_deleting_history(tmp_path: Path) -> None:
    database, service = setup_service(tmp_path, [flat_bar(0, 9), flat_bar(1, 9)])
    lot_id = add_stop(database)
    analyze(service, 0)
    archive_position_lot(
        database, lot_id=lot_id, confirmation=CONFIRM_ARCHIVE, reason="人工归档测试"
    )

    analyze(service, 1)

    with database.connect() as connection:
        event = connection.execute("SELECT * FROM rule_events WHERE event_type = 'fixed_stop'").fetchone()
        lot = connection.execute("SELECT * FROM position_lots").fetchone()
    assert event["status"] == "cleared" and lot["active"] == 0


@pytest.mark.parametrize(
    ("last_close", "event_type"),
    [(10.2, "rapid_rise"), (9.8, "rapid_fall")],
)
def test_rapid_move_rules_use_one_minute_return(
    tmp_path: Path, last_close: float, event_type: str
) -> None:
    database = Database(tmp_path / "rapid.db")
    database.initialize()
    ingest_minutes(
        database, [flat_bar(0, 10), flat_bar(1, last_close)], adapter="test"
    )
    service = AnalysisService(database, rules=RulesConfig(rapid_move_pct=1.0))

    result = analyze(service, 1)

    matching = [notice for notice in result.notices if notice.event_type == event_type]
    assert len(matching) == 1 and matching[0].notification_kind == "initial"
    opposite = "rapid_fall" if event_type == "rapid_rise" else "rapid_rise"
    assert all(notice.event_type != opposite for notice in result.notices)


@pytest.mark.parametrize(("last_close", "should_trigger"), [(10.1, False), (10.2, True)])
def test_volume_breakout_requires_strict_price_break_and_volume_threshold(
    tmp_path: Path, last_close: float, should_trigger: bool
) -> None:
    database = Database(tmp_path / "breakout.db")
    database.initialize()
    history_days = recent_weekdays(DAY, 21)[:-1]
    current = [
        MinuteBar(CODE, POINTS[index], 10, 10.1, 9.9, 10, 100, "test")
        for index in range(20)
    ]
    current.append(MinuteBar(
        CODE, POINTS[20], last_close, last_close + 0.1, last_close - 0.1,
        last_close, 300, "test",
    ))
    historical = [
        MinuteBar(
            CODE, POINTS[20].replace(year=day.year, month=day.month, day=day.day),
            8, 8.1, 7.9, 8, 100, "test",
        )
        for day in history_days
    ]
    ingest_minutes(database, [*historical, *current], adapter="test", check_gaps=False)
    open_days = set(history_days)
    calendar = []
    cursor = history_days[0]
    while cursor < DAY:
        calendar.append(TradingDay(cursor, cursor in open_days, "test"))
        cursor += timedelta(days=1)
    ingest_calendar(database, calendar, adapter="test")
    service = AnalysisService(database, rules=RulesConfig(
        rapid_move_pct=20.0, volume_ratio_threshold=1.5,
        breakout_lookback_minutes=20,
    ))

    result = analyze(service, 20)

    breakout = [notice for notice in result.notices if notice.event_type == "volume_breakout"]
    assert len(breakout) == int(should_trigger)
    with database.connect() as connection:
        snapshot = connection.execute(
            "SELECT values_json FROM indicator_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    values = json.loads(snapshot[0])
    assert values["prior_breakout_high"] == 10.1
    assert values["volume_ratio"] == 3.0
