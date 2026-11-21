"""C1 独立验收：虚拟 Tushare 响应 → 既有清洗入库 → 只读体检。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.storage.db import Database
from pandeng.storage.repositories import (
    IngestionSummary,
    ingest_calendar,
    ingest_daily,
    ingest_minutes,
)

from .audit import AuditReport, audit_market_data
from .response import ensure_calendar_scope, ensure_daily_scope, ensure_minute_scope
from .tushare_adapter import TushareFixtureAdapter


@dataclass(frozen=True)
class ProviderDemoOutcome:
    minutes: IngestionSummary
    daily: IngestionSummary
    calendar: IngestionSummary
    excluded_opening_auction: int
    minute_rows: int
    daily_rows: int
    calendar_rows: int
    audit: AuditReport


def _moment(day: date, clock_text: str) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{clock_text}+08:00")


def _require_fresh_database(database: Database) -> None:
    tables = (
        "minute_bars",
        "daily_qfq_bars",
        "trading_calendar",
        "ingestion_runs",
        "monitor_rounds",
        "analysis_runs",
        "summary_tasks",
        "position_lots",
    )
    with database.connect() as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    if any(counts.values()):
        raise ConfigError(
            "provider-fixture-demo 只使用全新的 real-data-test.db；本次没有覆盖或追加。"
            "请保留旧库，并在独立测试配置中指定一个不存在的新文件。"
        )


def run_provider_fixture_demo(
    config: AppConfig, session_date: date, fixture_file: Path
) -> ProviderDemoOutcome:
    database = Database(config.storage.database)
    if database.path.name != "real-data-test.db":
        raise ConfigError(
            "C1 离线演示只允许写入文件名 real-data-test.db，"
            "已拒绝使用主库或其他业务库。"
        )
    if config.ollama.enabled or config.cloud.enabled:
        raise ConfigError("C1 离线验收要求 Ollama 和云端均关闭。")
    _require_fresh_database(database)

    print("真实行情接入 C1 离线验收：只读取人工编写的虚拟 Tushare 响应。")
    print("没有 HTTP 客户端，不读取 Token，不请求真实行情、Ollama 或云端。")
    adapter = TushareFixtureAdapter(fixture_file)
    codes = tuple(item.code for item in config.watchlist)
    start = _moment(session_date, "09:31:00")
    end = _moment(session_date, "09:32:00")
    minutes = list(adapter.fetch_minute_bars(codes, start, end))
    daily = list(adapter.fetch_daily_qfq(codes, session_date, session_date))
    calendar = list(adapter.fetch_trading_calendar(session_date, session_date))

    # 在任何写入之前完成代码与请求闭区间防护。
    ensure_minute_scope(minutes, codes, start, end)
    ensure_daily_scope(daily, codes, session_date, session_date)
    ensure_calendar_scope(calendar, session_date, session_date)
    missing_minutes = sorted(set(codes).difference(
        str(record.get("stock_code")) for record in minutes
    ))
    missing_daily = sorted(set(codes).difference(
        str(record.get("stock_code")) for record in daily
    ))
    if missing_minutes or missing_daily or not calendar:
        raise ConfigError(
            "虚拟响应未覆盖当前配置："
            f"分钟缺少={missing_minutes}，日线缺少={missing_daily}，"
            f"日历记录={len(calendar)}。"
        )

    calendar_summary = ingest_calendar(database, calendar, adapter=adapter.name)
    minute_summary = ingest_minutes(
        database, minutes, adapter=adapter.name, replay_clock=end
    )
    daily_summary = ingest_daily(database, daily, adapter=adapter.name)
    print(
        f"1. 连续竞价分钟：收到 {minute_summary.received}；新增 {minute_summary.accepted}；"
        f"拒绝 {minute_summary.rejected}；范围 09:31～09:32。"
    )
    print(
        f"2. 开盘集合竞价 09:30：单独排除 {len(adapter.excluded_opening_auction)} 条；"
        "没有改写为 09:31。"
    )
    print(
        f"3. 明确 qfq 日线：收到 {daily_summary.received}；新增 {daily_summary.accepted}；"
        "confirmed=false，保持未确认，不能生成正式日线结论。"
    )
    print(
        f"4. 交易日历：收到 {calendar_summary.received}；新增 {calendar_summary.accepted}。"
    )
    audit = audit_market_data(database, codes, session_date, end)
    print(
        f"5. 只读体检：分钟总体 {audit.status}；完整 {audit.complete_count} 只；"
        f"未知 {audit.unknown_count} 只；日 K 状态请逐项查看。"
    )

    with database.connect() as connection:
        minute_rows = int(connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
        daily_rows = int(connection.execute("SELECT COUNT(*) FROM daily_qfq_bars").fetchone()[0])
        calendar_rows = int(connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0])
    print("原始服务商行已嵌入各记录的 raw_json.provider_raw，便于离线追溯。")
    print("未请求真实数据、未调用模型、未新增或修改真实持仓。")
    return ProviderDemoOutcome(
        minute_summary,
        daily_summary,
        calendar_summary,
        len(adapter.excluded_opening_auction),
        minute_rows,
        daily_rows,
        calendar_rows,
        audit,
    )
