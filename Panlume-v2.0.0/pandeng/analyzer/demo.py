"""第三阶段独立离线验收：指标、止损、冷却、恢复隔离和 MA20。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Iterable

from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import AppConfig, ConfigError
from pandeng.data.base import AdapterRecord, DailyBar, MarketDataAdapter, MinuteBar, TradingDay
from pandeng.data.schedule import recent_weekdays, session_minutes
from pandeng.data.simulator import SimulatedDataAdapter
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot, t1_availability
from pandeng.indicators.daily import analyze_daily
from pandeng.monitor.clock import ManualClock
from pandeng.monitor.service import MonitorService, RoundResult
from pandeng.rules.engine import RuleNotice
from pandeng.storage.db import Database
from pandeng.storage.monitor import recent_monitor_rounds
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes


class Stage3DemoAdapter(MarketDataAdapter):
    name = "simulator"

    def __init__(self, codes: tuple[str, ...], day: date, clock: ManualClock) -> None:
        self.codes = codes
        self.day = day
        self.clock = clock
        self.outages: set[datetime] = set()
        self.daily_confirmed = False
        self.minute_requests = 0
        self.points = session_minutes(day)
        self.bars = {code: [self._bar(code, index) for index in range(240)] for code in codes}

    def _bar(self, code: str, index: int) -> MinuteBar:
        point = self.points[index]
        if code == self.codes[0]:
            in_clear_window = time(9, 55) <= point.time() < time(10, 0)
            close = 14.0 if in_clear_window else 12.0
            return MinuteBar(code, point, close, close, close, close, 100, self.name)
        if len(self.codes) > 1 and code == self.codes[1]:
            return MinuteBar(code, point, 10.0, 10.0, 9.0, 9.0, 100, self.name)
        return MinuteBar(code, point, 10.0, 10.0, 10.0, 10.0, 100, self.name)

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        self.minute_requests += 1
        if self.clock.now() in self.outages:
            raise ConnectionError("第三阶段演示注入的断网")
        for code in stock_codes:
            for item in self.bars.get(code, []):
                if start <= item.bar_time <= end:
                    yield item

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        for code in stock_codes:
            if start <= self.day <= end:
                yield DailyBar(
                    code, self.day, 10.5, 11.2, 9.8, 11.0, 100000,
                    self.name, "qfq", self.daily_confirmed,
                )

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        cursor = start
        while cursor <= end:
            yield TradingDay(cursor, cursor.weekday() < 5, self.name)
            cursor = date.fromordinal(cursor.toordinal() + 1)


@dataclass(frozen=True)
class Stage3DemoOutcome:
    rounds: int
    analysis_runs: int
    snapshots: int
    notices: int
    recovery_checks: int
    daily_unknown_before_promotion: int
    formal_backfills_after_promotion: int


def _moment(day: date, text: str) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{text}+08:00")


def _print_round(label: str, result: RoundResult | None) -> None:
    if result is not None:
        print(
            f"{label}：{result.status}；新增 {result.accepted}；"
            f"可分析 {len(result.analysis_ready_codes)}；恢复={'是' if result.recovery else '否'}。"
        )


def _seed_history(database: Database, codes: tuple[str, ...], day: date) -> list[date]:
    all_days = recent_weekdays(day, 25)
    previous_days = all_days[:-1]
    baseline_days = previous_days[-20:]
    minute_adapter = SimulatedDataAdapter(codes, baseline_days)
    start = session_minutes(baseline_days[0])[0]
    end = session_minutes(baseline_days[-1])[-1]
    minute_summary = ingest_minutes(
        database,
        minute_adapter.fetch_minute_bars(codes, start, end),
        adapter="simulator", replay_clock=end,
    )
    if minute_summary.status != "ok":
        raise ConfigError("演示历史分钟基线未能通过连续性检查。")
    daily = [
        DailyBar(code, trade_day, 10, 10.2, 9.8, 10, 100000, "simulator", "qfq", True)
        for code in codes for trade_day in previous_days
    ]
    daily_summary = ingest_daily(database, daily, adapter="simulator")
    open_days = set(previous_days)
    calendar = []
    cursor = previous_days[0]
    while cursor < day:
        calendar.append(TradingDay(cursor, cursor in open_days, "simulator"))
        cursor = date.fromordinal(cursor.toordinal() + 1)
    calendar_summary = ingest_calendar(database, calendar, adapter="simulator")
    if daily_summary.status != "ok" or calendar_summary.status != "ok":
        raise ConfigError("演示历史日线或交易日历初始化失败。")
    print(
        f"历史基线：{len(baseline_days)} 个交易日的完整分钟线，"
        f"{len(previous_days)} 根已确认前复权日 K／股票。"
    )
    return previous_days


def run_analysis_demo(
    config: AppConfig,
    session_date: date,
    *,
    on_notice: Callable[[RuleNotice], None] | None = None,
) -> Stage3DemoOutcome:
    database = Database(config.storage.database)
    if database.path.name != "analysis-test.db":
        raise ConfigError(
            "analysis-demo 只能使用文件名严格为 analysis-test.db 的独立数据库；"
            "请使用 configs/analysis-test.yaml，不会允许在主库或 monitor-test 库写入演示持仓。"
        )
    if session_date.weekday() >= 5:
        raise ConfigError("analysis-demo 的 session-date 请选择周一至周五。")
    with database.connect() as connection:
        existing_rounds = connection.execute("SELECT COUNT(*) FROM monitor_rounds").fetchone()[0]
        existing_lots = connection.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0]
    if existing_rounds or existing_lots:
        raise ConfigError(
            "该 analysis-test.db 已有演示记录，为避免混合持仓和历史，本命令不会覆盖或追加另一场演示。"
        )

    codes = tuple(item.code for item in config.watchlist)
    _seed_history(database, codes, session_date)
    lot_id = add_position_lot(
        database, stock_code=codes[0], quantity=100, buy_date=session_date.isoformat(),
        stop_loss_price=13.0, confirmation=CONFIRM_ADD,
        confirmed_at=_moment(session_date, "09:30:00"),
    )
    print(f"仅在独立演示库添加模拟持仓批次 {lot_id}：{codes[0]}，100 股，固定止损 13.00。")

    clock = ManualClock(_moment(session_date, "09:35:00"))
    adapter = Stage3DemoAdapter(codes, session_date, clock)
    sectors = {item.code: item.sector_code for item in config.watchlist if item.sector_code}
    analyzer = AnalysisService(database, rules=config.rules, sector_map=sectors)
    notice_count = 0

    def on_ready(result: RoundResult) -> None:
        nonlocal notice_count
        analyses = analyzer.analyze_round(result)
        for analysis in analyses:
            for notice in analysis.notices:
                notice_count += 1
                print(
                    f"  【{notice.priority}/{notice.notification_kind}】"
                    f"{notice.event_type}：{notice.message}"
                )
                if on_notice is not None:
                    on_notice(notice)

    service = MonitorService(
        database, adapter, codes, session_date, clock=clock,
        interval_seconds=config.monitor.interval_seconds,
        fetch_timeout_seconds=config.monitor.fetch_timeout_seconds,
        close_timeout_seconds=config.monitor.close_timeout_seconds,
        on_analysis_ready=on_ready,
    )
    print("第三阶段离线验收开始：所有行情均为模拟数据，未调用 Ollama 或云端。")
    for label, stamp in (
        ("1. 09:35 首次止损与普通事件", "09:35:00"),
        ("2. 09:40 持续状态合并", "09:40:00"),
        ("3. 09:50 独立重复提醒", "09:50:00"),
        ("4. 09:55 止损解除", "09:55:00"),
        ("5. 10:00 再次触发立即提醒", "10:00:00"),
    ):
        clock.set(_moment(session_date, stamp))
        _print_round(label, service.tick().round_result)

    outage = _moment(session_date, "10:05:00")
    clock.set(outage)
    adapter.outages.add(outage)
    _print_round("6. 10:05 断网，分析门控关闭", service.tick().round_result)
    clock.set(_moment(session_date, "10:10:00"))
    adapter.outages.clear()
    before_recovery = notice_count
    recovered = service.tick().round_result
    recovery_notices = notice_count - before_recovery
    _print_round("7. 10:10 恢复，只写补记", recovered)
    print(f"  恢复轮次实时提醒 {recovery_notices} 条；触发状态仍保存为恢复检查。")

    clock.set(_moment(session_date, "15:00:00"))
    closed = service.tick()
    _print_round("8. 15:00 收盘，日 K 故意未确认", closed.round_result)
    print(f"  收盘日 K 状态：{closed.daily_status}；不会生成当时正式日线信号。")

    before = {
        code: analyze_daily(database, code, session_date, analysis_mode="replay")[-1]
        for code in codes
    }
    unknown_before = sum(item.formal_signal is None for item in before.values())

    adapter.daily_confirmed = True
    promotion = ingest_daily(
        database,
        adapter.fetch_daily_qfq(codes, session_date, session_date),
        adapter="simulator",
    )
    if promotion.status != "ok":
        raise ConfigError("演示日 K 确认推进失败。")
    final = {
        code: analyze_daily(database, code, session_date, analysis_mode="replay")[-1]
        for code in codes
    }
    formal_backfills = sum(item.formal_signal is True for item in final.values())
    availability = t1_availability(database, codes[0], session_date)
    print(
        f"9. 后续确认补记：未确认时正式信号未知 {unknown_before} 只；"
        f"确认后满足双条件 {formal_backfills} 只（全部为回放补记）。"
    )
    print(
        f"10. 当日买入 T+1：总记录数量 {availability.total_quantity}，"
        f"可卖 {availability.sellable_quantity}，状态 {availability.status}。"
    )

    with database.connect() as connection:
        analysis_runs = connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
        snapshots = connection.execute("SELECT COUNT(*) FROM indicator_snapshots").fetchone()[0]
        recovery_checks = connection.execute(
            "SELECT COUNT(*) FROM rule_event_checks WHERE notification_kind = 'recovery'"
        ).fetchone()[0]
    rounds = len(recent_monitor_rounds(database, limit=1000))
    print(
        f"验收汇总：调度轮次 {rounds}；分析运行 {analysis_runs}；指标快照 {snapshots}；"
        f"实时提醒 {notice_count}；恢复补记检查 {recovery_checks}。"
    )
    print("未访问真实行情、未修改主库真实持仓、未执行任何交易或外部账户操作。")
    return Stage3DemoOutcome(
        rounds, analysis_runs, snapshots, notice_count, recovery_checks,
        unknown_before, formal_backfills,
    )
