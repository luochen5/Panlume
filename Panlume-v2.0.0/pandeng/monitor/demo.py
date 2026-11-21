"""不实际等待的第二阶段 B 验收场景；全部使用本地模拟器。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Iterable

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.data.base import AdapterRecord, MarketDataAdapter
from pandeng.data.simulator import make_simulator
from pandeng.storage.db import Database
from pandeng.storage.monitor import (
    recent_monitor_events,
    recent_monitor_rounds,
    session_round_count,
)

from .clock import ManualClock
from .service import MonitorService, RoundResult


class DemoAdapter(MarketDataAdapter):
    """在模拟器上注入精确时刻的断网；不写网络、不访问真实行情。"""

    name = "simulator"

    def __init__(self, clock: ManualClock, codes: tuple[str, ...], day: date) -> None:
        self.clock = clock
        self.base = make_simulator(codes, day, daily_days=3)
        self.outages: set[datetime] = set()
        self.minute_requests = 0

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        self.minute_requests += 1
        if self.clock.now() in self.outages:
            raise ConnectionError("演示场景注入的断网")
        return self.base.fetch_minute_bars(stock_codes, start, end)

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        # 故意保留 confirmed=False：收盘本身不能替代日 K 确认。
        for item in self.base.fetch_daily_qfq(stock_codes, start, end):
            yield replace(item, confirmed=False)

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        return self.base.fetch_trading_calendar(start, end)


@dataclass(frozen=True)
class DemoOutcome:
    rounds: int
    offline_recovery_events: int
    daily_status: str
    minute_requests: int


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _print_round(label: str, result: RoundResult | None) -> None:
    if result is None:
        print(f"{label}：没有启动轮次。")
        return
    print(
        f"{label}：轮次 {result.round_id}；{result.status}；新增 {result.accepted}；"
        f"去重 {result.duplicates}；可分析代码 {len(result.analysis_ready_codes)}；"
        f"离线恢复={'是' if result.recovery else '否'}"
    )


def run_monitor_demo(config: AppConfig, session_date: date) -> DemoOutcome:
    database = Database(config.storage.database)
    if session_round_count(database, session_date.isoformat()):
        raise ConfigError(
            "该日期已经有调度验收记录。为避免覆盖历史，请换用新的 --session-date，"
            "或继续使用独立的 monitor-test 数据库。"
        )
    codes = tuple(item.code for item in config.watchlist)
    clock = ManualClock(_moment(f"{session_date.isoformat()}T09:35:00+08:00"))
    adapter = DemoAdapter(clock, codes, session_date)
    service = MonitorService(
        database, adapter, codes, session_date, clock=clock,
        interval_seconds=config.monitor.interval_seconds,
        fetch_timeout_seconds=config.monitor.fetch_timeout_seconds,
        close_timeout_seconds=config.monitor.close_timeout_seconds,
    )

    print("第二阶段 B 离线验收开始：使用模拟时钟，不等待真实午休、收盘或断网。")
    _print_round("1. 09:35 首轮增量", service.tick().round_result)

    clock.set(_moment(f"{session_date.isoformat()}T09:36:00+08:00"))
    service.pause()
    clock.set(_moment(f"{session_date.isoformat()}T09:40:00+08:00"))
    paused_requests = adapter.minute_requests
    paused = service.tick()
    print(
        f"2. 暂停期间：{paused.message}；"
        f"新增请求 {adapter.minute_requests - paused_requests} 次。"
    )
    clock.set(_moment(f"{session_date.isoformat()}T09:41:00+08:00"))
    _print_round("3. 09:41 继续并补跑", service.resume())

    outage_at = _moment(f"{session_date.isoformat()}T09:46:00+08:00")
    clock.set(outage_at)
    adapter.outages.add(outage_at)
    failed = service.tick().round_result
    _print_round("4. 09:46 注入断网", failed)
    print("  断网期间没有分析门控通过，状态明确记为未知。")

    clock.set(_moment(f"{session_date.isoformat()}T09:51:00+08:00"))
    adapter.outages.clear()
    recovered = service.tick().round_result
    _print_round("5. 09:51 网络恢复并补数", recovered)

    clock.set(_moment(f"{session_date.isoformat()}T12:00:00+08:00"))
    before_lunch = adapter.minute_requests
    lunch = service.tick()
    print(
        f"6. 午休：{lunch.message}；新增请求 {adapter.minute_requests - before_lunch} 次。"
    )
    clock.set(_moment(f"{session_date.isoformat()}T13:01:00+08:00"))
    _print_round("7. 13:01 续接上午检查点", service.tick().round_result)

    clock.set(_moment(f"{session_date.isoformat()}T15:00:00+08:00"))
    closed = service.tick()
    _print_round("8. 15:00 最后一轮", closed.round_result)
    print(f"9. 收盘收尾：{closed.message}")
    print("  演示日 K 故意未确认，因此正式日线结论必须保持状态未知。")
    print("未访问真实行情、未调用 Ollama 或云端、未修改真实持仓。")

    rounds = [
        row for row in recent_monitor_rounds(database, limit=1000)
        if row["session_date"] == session_date.isoformat()
    ]
    events = [
        row for row in recent_monitor_events(database, limit=1000)
        if row["session_date"] == session_date.isoformat()
    ]
    return DemoOutcome(
        rounds=len(rounds),
        offline_recovery_events=sum(
            row["event_type"] == "offline_recovery" for row in events
        ),
        daily_status=closed.daily_status or "unknown",
        minute_requests=adapter.minute_requests,
    )
