from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pytest

from pandeng.data.base import AdapterRecord, MarketDataAdapter
from pandeng.data.response import (
    AdapterResponseError,
    ensure_calendar_scope,
    ensure_daily_scope,
    ensure_minute_scope,
)
from pandeng.data.schedule import CHINA_TZ
from pandeng.data.simulator import make_simulator
from pandeng.monitor.clock import ManualClock
from pandeng.monitor.service import MonitorService
from pandeng.storage.db import Database
from test_cleaner import daily_record, minute_record


DAY = date(2026, 1, 12)
CODES = ("600000", "000001")


def moment(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-01-12T{text}+08:00")


def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "adapter-scope.db")
    db.initialize()
    return db


class ControlledAdapter(MarketDataAdapter):
    name = "controlled_offline"

    def __init__(self) -> None:
        self.base = make_simulator(CODES, DAY, daily_days=1)
        self.rogue_code = False
        self.empty_code = False

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        records = list(self.base.fetch_minute_bars(codes, start, end))
        if codes == ("600000",) and self.empty_code:
            return []
        if codes == ("600000",) and self.rogue_code:
            records.append(minute_record(
                stock_code="000001", bar_time=start.isoformat(), source="rogue"
            ))
        return records

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        return self.base.fetch_daily_qfq(stock_codes, start, end)

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        return self.base.fetch_trading_calendar(start, end)


def service(db: Database, adapter: ControlledAdapter) -> MonitorService:
    return MonitorService(
        db, adapter, CODES, DAY,
        clock=ManualClock(moment("09:32:00")), interval_seconds=60,
        fetch_timeout_seconds=2, close_timeout_seconds=5,
    )


def test_scope_guards_accept_valid_records_and_reject_cross_code_response() -> None:
    valid = minute_record(bar_time="2026-01-12T09:32:00+08:00")
    checked = ensure_minute_scope([valid], ["600000"], moment("09:31:00"), moment("09:32:00"))
    assert checked == [valid]

    with pytest.raises(AdapterResponseError, match="不在本次请求范围"):
        ensure_minute_scope(
            [minute_record(stock_code="000001")], ["600000"],
            moment("09:31:00"), moment("09:32:00"),
        )


def test_scope_guards_reject_out_of_range_minute_daily_and_calendar() -> None:
    with pytest.raises(AdapterResponseError, match="超出请求闭区间"):
        ensure_minute_scope(
            [minute_record(bar_time="2026-01-12T09:33:00+08:00")], ["600000"],
            moment("09:31:00"), moment("09:32:00"),
        )
    with pytest.raises(AdapterResponseError, match="超出请求闭区间"):
        ensure_daily_scope(
            [daily_record(trade_date="2026-01-13")], ["600000"], DAY, DAY
        )
    with pytest.raises(AdapterResponseError, match="超出请求闭区间"):
        ensure_calendar_scope(
            [{"trade_date": "2026-01-13", "is_open": True, "source": "test"}], DAY, DAY
        )


def test_scope_guard_leaves_malformed_time_for_audited_cleaner() -> None:
    malformed = minute_record(bar_time="not-a-time")
    result = ensure_minute_scope(
        [malformed], ["600000"], moment("09:31:00"), moment("09:32:00")
    )
    assert result == [malformed]


def test_one_rogue_stock_is_unknown_without_contaminating_other_stock(tmp_path: Path) -> None:
    db = database(tmp_path)
    adapter = ControlledAdapter()
    adapter.rogue_code = True

    result = service(db, adapter).tick().round_result

    assert result is not None and result.status == "unknown"
    assert result.analysis_ready_codes == ("000001",)
    rogue = next(item for item in result.code_results if item.stock_code == "600000")
    assert rogue.status == "unknown" and "不在本次请求范围内" in rogue.reason
    with db.connect() as connection:
        rows = list(connection.execute(
            "SELECT stock_code, COUNT(*) AS count FROM minute_bars GROUP BY stock_code"
        ))
        states = {
            row["stock_code"]: row["status"]
            for row in connection.execute("SELECT * FROM monitor_code_states")
        }
    assert [(row["stock_code"], row["count"]) for row in rows] == [("000001", 2)]
    assert states == {"600000": "unknown", "000001": "ok"}


def test_empty_incremental_response_is_unknown_and_checkpoint_does_not_advance(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    adapter = ControlledAdapter()
    monitor = service(db, adapter)
    first = monitor.run_round(now=moment("09:31:00"))
    assert first.status == "ok"

    adapter.empty_code = True
    second = monitor.run_round(now=moment("09:32:00"))

    empty = next(item for item in second.code_results if item.stock_code == "600000")
    healthy = next(item for item in second.code_results if item.stock_code == "000001")
    assert empty.status == "unknown" and not empty.analysis_allowed
    assert "仍缺分钟" in empty.reason
    assert healthy.status == "ok" and healthy.analysis_allowed
    with db.connect() as connection:
        checkpoints = {
            row["stock_code"]: row["last_contiguous_time"]
            for row in connection.execute("SELECT * FROM monitor_code_states")
        }
    assert checkpoints["600000"] == moment("09:31:00").isoformat()
    assert checkpoints["000001"] == moment("09:32:00").isoformat()
