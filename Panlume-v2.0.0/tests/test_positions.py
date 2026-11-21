from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pandeng.data.base import TradingDay
from pandeng.holdings.positions import (
    CONFIRM_ADD,
    CONFIRM_ARCHIVE,
    PositionError,
    add_position_lot,
    archive_position_lot,
    t1_availability,
)
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar


@pytest.fixture
def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "runtime" / "positions.db")
    result.initialize()
    return result


def add_lot(database: Database, **overrides) -> int:
    values = {
        "stock_code": "600000",
        "quantity": 100,
        "buy_date": "2026-01-05",
        "stop_loss_price": 9.5,
        "confirmation": CONFIRM_ADD,
        "confirmed_at": datetime(2026, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    }
    values.update(overrides)
    return add_position_lot(database, **values)


def save_calendar(database: Database, rows: list[tuple[str, bool]]) -> None:
    summary = ingest_calendar(
        database,
        [TradingDay(date.fromisoformat(day), is_open, "test-calendar") for day, is_open in rows],
        adapter="test-calendar",
    )
    assert summary.status == "ok"


def test_add_requires_exact_confirmation_and_writes_audit(database: Database) -> None:
    with pytest.raises(PositionError, match="必须逐字确认"):
        add_lot(database, confirmation="确认")
    assert database.list_position_lots() == []

    lot_id = add_lot(database)

    lots = database.list_position_lots()
    assert len(lots) == 1 and lots[0]["id"] == lot_id and lots[0]["active"] == 1
    with database.connect() as connection:
        audit = connection.execute("SELECT * FROM position_audit").fetchone()
    assert audit["action"] == "add" and audit["before_json"] is None
    assert audit["confirmation_text"] == CONFIRM_ADD
    assert json.loads(audit["after_json"])["quantity"] == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stock_code", "1"),
        ("quantity", 0),
        ("quantity", 150),
        ("quantity", True),
        ("buy_date", "2026/01/05"),
        ("stop_loss_price", 0),
        ("stop_loss_price", float("nan")),
    ],
)
def test_invalid_lot_input_writes_nothing(database: Database, field: str, value) -> None:
    with pytest.raises(PositionError):
        add_lot(database, **{field: value})
    assert database.list_position_lots() == []


def test_archive_preserves_lot_and_rejects_repeat(database: Database) -> None:
    lot_id = add_lot(database)
    with pytest.raises(PositionError, match="必须逐字确认"):
        archive_position_lot(database, lot_id=lot_id, confirmation="归档", reason="测试")
    with pytest.raises(PositionError, match="归档原因"):
        archive_position_lot(
            database, lot_id=lot_id, confirmation=CONFIRM_ARCHIVE, reason=None
        )

    archive_position_lot(
        database, lot_id=lot_id, confirmation=CONFIRM_ARCHIVE, reason="人工核对：该记录已不需要参与当前持仓"
    )

    lot = database.list_position_lots()[0]
    assert lot["quantity"] == 100 and lot["active"] == 0
    assert lot["deactivation_reason"].startswith("人工核对")
    with pytest.raises(PositionError, match="已归档"):
        archive_position_lot(
            database, lot_id=lot_id, confirmation=CONFIRM_ARCHIVE, reason="重复"
        )
    with database.connect() as connection:
        actions = [row[0] for row in connection.execute(
            "SELECT action FROM position_audit ORDER BY id"
        )]
    assert actions == ["add", "archive"]


def test_t1_same_day_then_next_open_day(database: Database) -> None:
    add_lot(database, buy_date="2026-01-02")  # 周五
    save_calendar(database, [
        ("2026-01-02", True), ("2026-01-03", False),
        ("2026-01-04", False), ("2026-01-05", True),
    ])

    same_day = t1_availability(database, "600000", date(2026, 1, 2))
    monday = t1_availability(database, "600000", date(2026, 1, 5))

    assert same_day.sellable_quantity == 0 and same_day.status == "ok"
    assert monday.sellable_quantity == 100 and monday.status == "ok"
    assert monday.lots[0].next_open_date == "2026-01-05"


def test_t1_all_intervening_days_closed_is_not_yet_sellable(database: Database) -> None:
    add_lot(database, buy_date="2026-01-02")
    save_calendar(database, [
        ("2026-01-02", True), ("2026-01-03", False), ("2026-01-04", False),
    ])

    result = t1_availability(database, "600000", date(2026, 1, 4))

    assert result.status == "ok" and result.sellable_quantity == 0
    assert "明确休市" in result.lots[0].reason


def test_t1_missing_buy_day_or_intervening_calendar_is_unknown(database: Database) -> None:
    add_lot(database, buy_date="2026-01-02")
    save_calendar(database, [("2026-01-03", False), ("2026-01-05", True)])
    assert t1_availability(database, "600000", date(2026, 1, 5)).status == "unknown"

    other = Database(database.path.parent / "missing-middle.db")
    other.initialize()
    add_lot(other, buy_date="2026-01-02")
    save_calendar(other, [("2026-01-02", True), ("2026-01-04", False)])
    result = t1_availability(other, "600000", date(2026, 1, 4))
    assert result.status == "unknown" and result.sellable_quantity is None
    assert "2026-01-03" in result.lots[0].reason


def test_t1_closed_buy_day_and_future_buy_day_are_unknown(database: Database) -> None:
    add_lot(database, buy_date="2026-01-03")
    save_calendar(database, [("2026-01-03", False), ("2026-01-04", False), ("2026-01-05", True)])
    assert t1_availability(database, "600000", date(2026, 1, 5)).status == "unknown"

    add_lot(database, buy_date="2026-01-06", quantity=200)
    result = t1_availability(database, "600000", date(2026, 1, 5))
    assert result.status == "unknown" and result.sellable_quantity is None
    assert len(result.lots) == 2


def test_t1_multiple_lots_and_archived_lot(database: Database) -> None:
    old = add_lot(database, buy_date="2026-01-02", quantity=100)
    add_lot(database, buy_date="2026-01-05", quantity=200, stop_loss_price=None)
    archived = add_lot(database, buy_date="2026-01-02", quantity=300)
    archive_position_lot(
        database, lot_id=archived, confirmation=CONFIRM_ARCHIVE, reason="测试归档"
    )
    save_calendar(database, [
        ("2026-01-02", True), ("2026-01-03", False),
        ("2026-01-04", False), ("2026-01-05", True),
    ])

    result = t1_availability(database, "600000", date(2026, 1, 5))

    assert result.total_quantity == 300 and result.sellable_quantity == 100
    assert [item.lot_id for item in result.lots] == [old, old + 1]
    assert [item.sellable_quantity for item in result.lots] == [100, 0]


def test_t1_empty_position_is_zero_not_unknown(database: Database) -> None:
    result = t1_availability(database, "600000", date(2026, 1, 5))
    assert result.status == "ok" and result.total_quantity == 0
    assert result.sellable_quantity == 0 and result.lots == ()
