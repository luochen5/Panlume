from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

import pytest

from pandeng.data.base import AdapterRecord, MarketDataAdapter
from pandeng.data.schedule import CHINA_TZ
from pandeng.data.simulator import make_simulator
from pandeng.monitor.clock import ManualClock
from pandeng.monitor.service import MonitorService, RoundResult
from pandeng.storage.db import Database
from pandeng.storage.monitor import (
    get_code_states,
    get_monitor_state,
    recent_monitor_events,
    recent_monitor_rounds,
)
from pandeng.storage.repositories import table_count


CODES = ("600000", "000001")
DAY = date(2026, 1, 5)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(CHINA_TZ)


class ControlledAdapter(MarketDataAdapter):
    name = "controlled-simulator"

    def __init__(self, clock: ManualClock, codes: Iterable[str] = CODES) -> None:
        self.clock = clock
        self.base = make_simulator(codes, DAY, daily_days=3)
        self.outages: set[datetime] = set()
        self.failed_codes: set[str] = set()
        self.drop_once: set[tuple[str, str]] = set()
        self.minute_calls: list[tuple[datetime, str, datetime, datetime]] = []
        self.daily_calls = 0
        self.daily_confirmed = True
        self.daily_delay = 0.0
        self.on_minute: Callable[[str], None] | None = None

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        for code in stock_codes:
            self.minute_calls.append((self.clock.now(), code, start, end))
            if self.on_minute is not None:
                self.on_minute(code)
            if self.clock.now() in self.outages or code in self.failed_codes:
                raise ConnectionError(f" injected outage for {code} ")
            for item in self.base.fetch_minute_bars([code], start, end):
                key = (item.stock_code, item.bar_time.isoformat())
                if key in self.drop_once:
                    self.drop_once.remove(key)
                    continue
                yield item

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        self.daily_calls += 1
        if self.daily_delay:
            time.sleep(self.daily_delay)
        for item in self.base.fetch_daily_qfq(stock_codes, start, end):
            if item.trade_date == DAY:
                yield replace(item, confirmed=self.daily_confirmed, source=self.name)
            else:
                yield item

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        return self.base.fetch_trading_calendar(start, end)


def make_service(
    tmp_path: Path,
    *,
    start: str = "2026-01-05T09:35:00+08:00",
    clock: ManualClock | None = None,
    daily_confirmed: bool = True,
    on_analysis_ready=None,
    on_daily_confirmed=None,
) -> tuple[Database, ManualClock, ControlledAdapter, MonitorService]:
    database = Database(tmp_path / "runtime" / "monitor.db")
    database.initialize()
    clock = clock or ManualClock(at(start))
    adapter = ControlledAdapter(clock)
    adapter.daily_confirmed = daily_confirmed
    service = MonitorService(
        database, adapter, CODES, DAY, clock=clock,
        fetch_timeout_seconds=1, close_timeout_seconds=1,
        on_analysis_ready=on_analysis_ready,
        on_daily_confirmed=on_daily_confirmed,
    )
    return database, clock, adapter, service


def test_incremental_rounds_fetch_only_new_minutes(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    first = service.tick().round_result
    assert first is not None and first.accepted == 10

    clock.set(at("2026-01-05T09:40:00+08:00"))
    second = service.tick().round_result

    assert second is not None and second.accepted == 10 and second.duplicates == 0
    assert all(item.start == at("2026-01-05T09:36:00+08:00") for item in second.code_results)
    assert table_count(database, "minute_bars") == 20


def test_pause_resume_backfills_and_marks_offline_recovery(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    initial_calls = len(adapter.minute_calls)

    clock.set(at("2026-01-05T09:36:00+08:00"))
    service.pause()
    clock.set(at("2026-01-05T09:40:00+08:00"))
    assert service.tick().action == "paused"
    assert len(adapter.minute_calls) == initial_calls

    clock.set(at("2026-01-05T09:41:00+08:00"))
    resumed = service.resume()

    assert resumed.recovery is True and resumed.accepted == 12
    assert resumed.status == "ok"
    events = recent_monitor_events(database)
    offline = [row for row in events if row["event_type"] == "offline_recovery"]
    assert len(offline) == 2
    assert all("不能冒充当时实时告警" in row["message"] for row in offline)
    assert table_count(database, "minute_bars") == 22


def test_network_recovery_completes_data_before_analysis(tmp_path: Path) -> None:
    ready_calls: list[tuple[str, ...]] = []
    database, clock, adapter, service = make_service(
        tmp_path, on_analysis_ready=lambda result: ready_calls.append(result.analysis_ready_codes)
    )
    service.tick()
    calls_after_first = len(ready_calls)

    failed_at = at("2026-01-05T09:40:00+08:00")
    clock.set(failed_at)
    adapter.outages.add(failed_at)
    failed = service.tick().round_result
    assert failed is not None and failed.status == "unknown"
    assert failed.analysis_ready_codes == ()
    assert len(ready_calls) == calls_after_first

    clock.set(at("2026-01-05T09:45:00+08:00"))
    adapter.outages.clear()
    recovered = service.tick().round_result

    assert recovered is not None and recovered.recovery and recovered.accepted == 20
    assert recovered.analysis_ready_codes == CODES
    assert len(ready_calls) == calls_after_first + 1
    states = get_code_states(database, DAY.isoformat())
    assert all(row["status"] == "ok" for row in states.values())


def test_incomplete_recovery_stays_unknown_until_gap_is_filled(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    adapter.drop_once.add(("600000", "2026-01-05T09:38:00+08:00"))

    clock.set(at("2026-01-05T09:40:00+08:00"))
    incomplete = service.tick().round_result
    assert incomplete is not None
    assert incomplete.analysis_ready_codes == CODES
    first_code = next(item for item in incomplete.code_results if item.stock_code == "600000")
    assert first_code.status == "unknown" and first_code.analysis_allowed is True
    assert "仅价格线可判定 1 只" in incomplete.message
    states = get_code_states(database, DAY.isoformat())
    assert states["600000"]["last_contiguous_time"] == "2026-01-05T09:35:00+08:00"
    assert "09:38" in states["600000"]["unknown_reason"]

    clock.set(at("2026-01-05T09:45:00+08:00"))
    recovered = service.tick().round_result
    assert recovered is not None and recovered.analysis_ready_codes == CODES
    assert get_code_states(database, DAY.isoformat())["600000"]["status"] == "ok"


def test_single_code_failure_does_not_block_healthy_code(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    adapter.failed_codes.add("600000")
    clock.set(at("2026-01-05T09:40:00+08:00"))

    result = service.tick().round_result

    assert result is not None and result.analysis_ready_codes == ("000001",)
    states = get_code_states(database, DAY.isoformat())
    assert states["600000"]["status"] == "unknown"
    assert states["000001"]["status"] == "ok"


def test_lunch_has_no_requests_and_afternoon_backfills_morning(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    calls_before_lunch = len(adapter.minute_calls)

    clock.set(at("2026-01-05T12:00:00+08:00"))
    assert service.tick().action == "lunch"
    assert len(adapter.minute_calls) == calls_before_lunch

    clock.set(at("2026-01-05T13:01:00+08:00"))
    afternoon = service.tick().round_result

    assert afternoon is not None and afternoon.accepted == 232
    assert all(
        item.start == at("2026-01-05T09:36:00+08:00")
        and item.end == at("2026-01-05T13:01:00+08:00")
        for item in afternoon.code_results
    )
    assert all(call[0].time().hour != 12 for call in adapter.minute_calls)
    assert table_count(database, "minute_bars") == 242


def test_close_finishes_and_unconfirmed_daily_stays_unknown(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path, daily_confirmed=False)
    service.tick()
    clock.set(at("2026-01-05T15:00:00+08:00"))

    closed = service.tick()

    assert closed.action == "closed" and closed.daily_status == "unknown"
    assert closed.round_result is not None
    assert closed.round_result.target == at("2026-01-05T15:00:00+08:00")
    state = get_monitor_state(database)
    assert state["status"] == "closed" and state["stop_reason"] == "session_closed"
    with database.connect() as connection:
        confirmations = [row[0] for row in connection.execute(
            "SELECT confirmed FROM daily_qfq_bars WHERE trade_date = ?", (DAY.isoformat(),)
        )]
    assert confirmations == [0, 0]
    assert any(
        row["event_type"] == "daily_unconfirmed" for row in recent_monitor_events(database)
    )
    calls = len(adapter.minute_calls)
    assert service.tick().action == "closed"
    assert len(adapter.minute_calls) == calls


def test_close_daily_wait_has_finite_timeout(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path, start="2026-01-05T15:00:00+08:00")
    adapter.daily_delay = 0.3
    service.close_timeout_seconds = 0.02

    started = time.monotonic()
    closed = service.tick()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert closed.action == "closed" and closed.daily_status == "unknown"
    assert get_monitor_state(database)["status"] == "closed"


class InterruptClock(ManualClock):
    def sleep(self, seconds: float) -> None:
        raise KeyboardInterrupt


def test_ctrl_c_during_wait_saves_checkpoint_and_stops(tmp_path: Path) -> None:
    clock = InterruptClock(at("2026-01-05T09:00:00+08:00"))
    database, _, adapter, service = make_service(tmp_path, clock=clock)

    assert service.run() == "ctrl_c"

    state = get_monitor_state(database)
    assert state["status"] == "stopped" and state["stop_reason"] == "ctrl_c"
    assert adapter.minute_calls == []
    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM run_logs WHERE event = 'monitor_stopped'"
        ).fetchone() is not None


def test_round_is_not_reentrant(tmp_path: Path) -> None:
    database, _, adapter, service = make_service(tmp_path)
    nested: list[RoundResult] = []
    called = False

    def reenter(_: str) -> None:
        nonlocal called
        if not called:
            called = True
            nested.append(service.run_round())

    adapter.on_minute = reenter
    result = service.tick().round_result

    assert result is not None and result.status == "ok"
    assert len(nested) == 1 and nested[0].status == "skipped"
    assert "上一轮尚未结束" in nested[0].message
    assert len(recent_monitor_rounds(database)) == 1


def test_simulated_full_session_runs_without_real_waiting(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(
        tmp_path, start="2026-01-05T09:31:00+08:00"
    )

    assert service.run() == "session_closed"

    assert clock.now() == at("2026-01-05T15:00:00+08:00")
    assert table_count(database, "minute_bars") == 480
    state = get_monitor_state(database)
    assert state["status"] == "closed" and state["daily_status"] == "ok"
    assert all(
        call[0].time() <= at("2026-01-05T11:30:00+08:00").time()
        or call[0].time() >= at("2026-01-05T13:01:00+08:00").time()
        for call in adapter.minute_calls
    )


def test_non_trading_day_and_after_close_do_not_poll(tmp_path: Path) -> None:
    saturday = date(2026, 1, 10)
    database = Database(tmp_path / "weekend.db")
    database.initialize()
    clock = ManualClock(at("2026-01-10T09:35:00+08:00"))
    adapter = ControlledAdapter(clock)
    adapter.base = make_simulator(CODES, saturday, daily_days=3)
    service = MonitorService(database, adapter, CODES, saturday, clock=clock)

    assert service.tick().round_result.status == "skipped"
    assert adapter.minute_calls == []
    assert get_monitor_state(database)["stop_reason"] == "not_trading_day"
    assert get_monitor_state(database)["next_poll_at"] is None

    other = Database(tmp_path / "after-close.db")
    other.initialize()
    clock2 = ManualClock(at("2026-01-05T16:00:00+08:00"))
    adapter2 = ControlledAdapter(clock2)
    service2 = MonitorService(other, adapter2, CODES, DAY, clock=clock2)
    assert service2.tick().action == "closed"
    assert adapter2.minute_calls == [] and adapter2.daily_calls == 0


def test_ctrl_c_during_round_saves_committed_data_and_stop_state(
    tmp_path: Path, monkeypatch
) -> None:
    database, _, _, service = make_service(tmp_path)
    import pandeng.monitor.service as service_module

    original = service_module.ingest_minutes
    interrupted = False

    def interrupt_after_commit(*args, **kwargs):
        nonlocal interrupted
        summary = original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return summary

    monkeypatch.setattr(service_module, "ingest_minutes", interrupt_after_commit)
    with pytest.raises(KeyboardInterrupt):
        service.tick()

    state = get_monitor_state(database)
    assert state["status"] == "stopped" and state["stop_reason"] == "ctrl_c"
    assert table_count(database, "minute_bars") == 5
    rounds = recent_monitor_rounds(database)
    assert len(rounds) == 1 and rounds[0]["status"] == "interrupted"


def test_restart_preserves_pause_and_resume_backfills(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    clock.set(at("2026-01-05T09:36:00+08:00"))
    service.pause()

    restarted = MonitorService(
        database, adapter, CODES, DAY, clock=clock,
        fetch_timeout_seconds=1, close_timeout_seconds=1,
    )
    clock.set(at("2026-01-05T09:41:00+08:00"))
    assert restarted.tick().action == "paused"
    resumed = restarted.resume()

    assert resumed.status == "ok" and resumed.accepted == 12
    assert table_count(database, "minute_bars") == 22


def test_close_with_network_still_down_records_unknown_and_exits(tmp_path: Path) -> None:
    database, clock, adapter, service = make_service(tmp_path)
    service.tick()
    close_at = at("2026-01-05T15:00:00+08:00")
    clock.set(close_at)
    adapter.outages.add(close_at)

    closed = service.tick()

    assert closed.action == "closed"
    assert closed.round_result is not None and closed.round_result.status == "unknown"
    assert get_monitor_state(database)["stop_reason"] == "session_closed"
    unknown = [row for row in recent_monitor_events(database) if row["event_type"] == "data_unknown"]
    assert len(unknown) == 2 and all("ConnectionError" in row["message"] for row in unknown)


def test_daily_confirmation_hook_runs_once_only_after_confirmed_close(tmp_path: Path) -> None:
    calls: list[tuple[date, bool]] = []
    database, _, _, service = make_service(
        tmp_path,
        start="2026-01-05T15:00:00+08:00",
        on_daily_confirmed=lambda day, recovery: calls.append((day, recovery)),
    )

    closed = service.tick()
    repeated = service.tick()

    assert closed.daily_status == "ok" and calls == [(DAY, False)]
    assert repeated.action == "closed" and calls == [(DAY, False)]


def test_daily_confirmation_hook_error_does_not_undo_saved_daily_bars(tmp_path: Path) -> None:
    def broken_hook(_: date, __: bool) -> None:
        raise RuntimeError("测试日线分析入口故障")

    database, _, _, service = make_service(
        tmp_path,
        start="2026-01-05T15:00:00+08:00",
        on_daily_confirmed=broken_hook,
    )

    closed = service.tick()

    assert closed.daily_status == "ok"
    with database.connect() as connection:
        confirmations = [row[0] for row in connection.execute(
            "SELECT confirmed FROM daily_qfq_bars WHERE trade_date = ?", (DAY.isoformat(),)
        )]
    assert confirmations == [1, 1]
    errors = [
        row for row in recent_monitor_events(database)
        if row["event_type"] == "daily_analysis_hook_error"
    ]
    assert len(errors) == 1 and "测试日线分析入口故障" in errors[0]["message"]
