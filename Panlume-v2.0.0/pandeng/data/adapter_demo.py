"""真实行情适配器离线验收：严格 JSON 样例、既有调度器和只读数据体检。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.storage.db import Database
from pandeng.storage.monitor import get_code_states
from pandeng.storage.repositories import ingest_daily

from .json_feed import JsonFeedAdapter, MinuteRequest
from .audit import AuditReport, audit_market_data
from pandeng.monitor.clock import ManualClock
from pandeng.monitor.service import MonitorService, RoundResult


@dataclass(frozen=True)
class AdapterDemoOutcome:
    rounds: tuple[RoundResult, ...]
    requests: tuple[MinuteRequest, ...]
    daily_accepted: int
    minute_rows: int
    daily_rows: int
    calendar_rows: int
    audit: AuditReport


def _moment(day: date, clock_text: str) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{clock_text}+08:00")


def _require_fresh_database(database: Database) -> None:
    tables = (
        "minute_bars", "daily_qfq_bars", "trading_calendar",
        "monitor_rounds", "analysis_runs", "summary_tasks",
    )
    with database.connect() as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    if any(counts.values()):
        raise ConfigError(
            "adapter-demo 只使用全新的 adapter-test.db；本次没有覆盖或追加。"
            "请保留旧库，并在独立测试配置中指定一个不存在的新文件。"
        )


def run_adapter_demo(
    config: AppConfig, session_date: date, feed_file: Path
) -> AdapterDemoOutcome:
    database = Database(config.storage.database)
    if database.path.name != "adapter-test.db":
        raise ConfigError(
            "离线适配器演示只允许写入文件名 adapter-test.db，已拒绝使用主库或其他业务库。"
        )
    if config.ollama.enabled or config.cloud.enabled:
        raise ConfigError("适配器离线验收要求 Ollama 和云端均关闭。")
    _require_fresh_database(database)

    codes = tuple(item.code for item in config.watchlist)
    adapter = JsonFeedAdapter(feed_file)
    available_codes = {bar.stock_code for bar in adapter.minute_bars}
    missing_codes = sorted(set(codes) - available_codes)
    if missing_codes:
        raise ConfigError(f"JSON 样例缺少配置所需代码：{', '.join(missing_codes)}")

    clock = ManualClock(_moment(session_date, "09:34:00"))
    service = MonitorService(
        database,
        adapter,
        codes,
        session_date,
        clock=clock,
        interval_seconds=config.monitor.interval_seconds,
        fetch_timeout_seconds=config.monitor.fetch_timeout_seconds,
        close_timeout_seconds=config.monitor.close_timeout_seconds,
    )
    print("真实行情适配器 B 离线验收开始：仅读取本地 JSON，不访问网络。")
    first = service.tick().round_result
    if first is None:
        raise ConfigError("第一轮没有启动，无法完成适配器验收。")
    print(
        f"1. 09:31～09:34 首轮：状态 {first.status}；收到 {first.received}；"
        f"新增 {first.accepted}；可分析 {len(first.analysis_ready_codes)} 只。"
    )

    clock.set(_moment(session_date, "09:36:00"))
    second = service.tick().round_result
    if second is None:
        raise ConfigError("第二轮没有启动，无法完成增量验收。")
    print(
        f"2. 09:35～09:36 增量：状态 {second.status}；收到 {second.received}；"
        f"新增 {second.accepted}；可分析 {len(second.analysis_ready_codes)} 只。"
    )
    for request in adapter.minute_requests:
        print(
            f"  只读请求：{','.join(request.stock_codes)}；"
            f"{request.start.isoformat()} 至 {request.end.isoformat()}"
        )

    daily = ingest_daily(
        database,
        adapter.fetch_daily_qfq(codes, session_date, session_date),
        adapter=adapter.name,
    )
    print(
        f"3. 当日明确前复权日 K：收到 {daily.received}；新增 {daily.accepted}；"
        f"状态 {daily.status}。本步骤不是收盘确认流程。"
    )

    states = get_code_states(database, session_date.isoformat())
    for code in codes:
        row = states.get(code)
        checkpoint = row["last_contiguous_time"] if row is not None else None
        print(f"4. {code} 连续检查点：{checkpoint or '未知'}")
    print("范围外响应会在入库前拒绝；空响应由既有连续性门控记为状态未知。")
    audit = audit_market_data(
        database, codes, session_date, _moment(session_date, "09:36:00")
    )
    print(
        f"5. 只读数据体检：分钟总体 {audit.status}；完整 {audit.complete_count} 只；"
        f"仅价格线可用 {audit.price_only_count} 只；状态未知 {audit.unknown_count} 只。"
    )
    print("未访问真实行情、未调用 Ollama 或云端、未修改真实持仓。")

    with database.connect() as connection:
        minute_rows = int(connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
        daily_rows = int(connection.execute("SELECT COUNT(*) FROM daily_qfq_bars").fetchone()[0])
        calendar_rows = int(connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0])
    return AdapterDemoOutcome(
        (first, second), tuple(adapter.minute_requests), daily.accepted,
        minute_rows, daily_rows, calendar_rows, audit,
    )
