"""盘灯命令行：离线数据、模拟调度、指标规则、持仓与日线分析。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from pandeng.config.loader import load_config
from pandeng.config.schema import (
    AppConfig,
    ConfigError,
    POLLING_PRESETS,
    polling_interval_label,
)
from pandeng.analyzer.demo import run_analysis_demo
from pandeng.analyzer.service import AnalysisResult, AnalysisService
from pandeng.data.adapter_demo import run_adapter_demo
from pandeng.data.akshare_demo import (
    run_akshare_live_minute,
    run_eastmoney_fixture_demo,
)
from pandeng.data.intraday_probe import (
    run_akshare_intraday_probe,
    run_eastmoney_intraday_fixture_demo,
)
from pandeng.data.audit import AuditReport, audit_market_data
from pandeng.data.provider_demo import run_provider_fixture_demo
from pandeng.data.replay import CsvReplayAdapter
from pandeng.data.schedule import CHINA_TZ
from pandeng.data.simulator import demo_time_range, make_simulator
from pandeng.holdings.positions import (
    add_position_lot,
    archive_position_lot,
    t1_availability,
)
from pandeng.indicators.daily import analyze_daily
from pandeng.monitor.demo import run_monitor_demo
from pandeng.storage.db import Database
from pandeng.storage.monitor import (
    get_code_states,
    get_monitor_state,
    recent_monitor_events,
    recent_monitor_rounds,
)
from pandeng.storage.repositories import (
    IngestionSummary,
    ingest_calendar,
    ingest_daily,
    ingest_minutes,
    latest_minutes,
    recent_runs,
    table_count,
)
from pandeng.summarizer.demo import run_summary_demo
from pandeng.summarizer.service import SummaryStore
from pandeng.review.cloud import CLOUD_REVIEW_CONFIRMATION, CloudReviewService
from pandeng.review.service import create_local_review, load_review, recent_reviews


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pandeng",
        description="盘灯 MVP：本地配置、SQLite、模拟回放、指标规则、摘要与受控行情适配器。",
    )
    parser.add_argument(
        "--config",
        default="configs/settings.yaml",
        help="YAML 配置路径，默认 configs/settings.yaml。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="校验配置并初始化或升级 SQLite。")
    commands.add_parser("show-config", help="显示已校验配置，不显示密钥。")
    commands.add_parser("status", help="查看数据库、自选股和模型开关状态。")

    holdings = commands.add_parser("holdings", help="人工确认持仓批次、归档与 T+1 查询。")
    holdings_commands = holdings.add_subparsers(dest="holdings_command", required=True)
    holdings_commands.add_parser("list", help="列出手动确认的持仓批次，包括已归档记录。")
    holding_add = holdings_commands.add_parser("add", help="显式确认后新增一个真实持仓批次。")
    holding_add.add_argument("--code", required=True, help="带引号的 6 位股票代码。")
    holding_add.add_argument("--quantity", required=True, type=int, help="100 股整数倍。")
    holding_add.add_argument("--buy-date", required=True, help="YYYY-MM-DD。")
    holding_add.add_argument("--stop-loss", type=float, help="固定止损价；省略表示未设置。")
    holding_add.add_argument("--confirmation", required=True, help="必须与程序提示的确认文本逐字一致。")
    holding_archive = holdings_commands.add_parser("archive", help="显式确认后归档批次，不删除历史。")
    holding_archive.add_argument("--lot-id", required=True, type=int)
    holding_archive.add_argument("--reason", required=True)
    holding_archive.add_argument("--confirmation", required=True)
    holding_t1 = holdings_commands.add_parser("t1", help="按交易日历逐批查询 T+1 可卖数量。")
    holding_t1.add_argument("--code", required=True)
    holding_t1.add_argument("--as-of-date", required=True, help="YYYY-MM-DD。")

    simulate = commands.add_parser("simulate", help="离线生成并保存模拟行情。")
    simulate.add_argument("--session-date", default="2026-01-05", help="模拟交易日。")
    simulate.add_argument("--minutes", type=int, default=5, help="每只股票的分钟数，1～240。")
    simulate.add_argument("--daily-days", type=int, default=25, help="每只股票的前复权日线数，1～60。")

    replay = commands.add_parser("replay", help="读取 CSV 并保存，不启动实时轮询。")
    replay.add_argument("--minute-file", type=Path)
    replay.add_argument("--daily-file", type=Path)
    replay.add_argument("--calendar-file", type=Path)
    replay.add_argument(
        "--replay-clock",
        help="可选的回放时钟，例如 2026-01-05T09:35:00+08:00。",
    )

    commands.add_parser("data-status", help="查看最近的数据处理批次与最新分钟行情。")
    data_audit = commands.add_parser(
        "data-audit", help="只读体检分钟缺口、价格线、质量冲突、检查点和日 K。"
    )
    data_audit.add_argument("--session-date", required=True, help="YYYY-MM-DD。")
    data_audit.add_argument(
        "--as-of-time", required=True,
        help="带 +08:00 的当日交易整分钟；体检不会使用当前电脑时间替代。",
    )
    data_audit.add_argument("--code", help="省略时检查配置中的全部自选股。")

    adapter_demo = commands.add_parser(
        "adapter-demo", help="在独立 adapter-test.db 离线验收三类适配器接口和增量读取。"
    )
    adapter_demo.add_argument("--session-date", default="2026-01-12", help="JSON 样例交易日。")
    adapter_demo.add_argument(
        "--feed-file", type=Path, default=Path("examples/market_feed.json"),
        help="严格 JSON 行情样例；只读，不访问网络。",
    )

    provider_demo = commands.add_parser(
        "provider-fixture-demo",
        help="在独立 real-data-test.db 验收虚拟 Tushare 响应映射，不联网。",
    )
    provider_demo.add_argument("--session-date", default="2026-01-12", help="虚拟样例交易日。")
    provider_demo.add_argument(
        "--fixture-file",
        type=Path,
        default=Path("examples/provider_fixtures/tushare/offline_responses.json"),
        help="人工编写的 Tushare 虚拟响应；不包含 Token 或真实行情。",
    )

    eastmoney_demo = commands.add_parser(
        "eastmoney-fixture-demo",
        help="在独立 akshare-fixture-test.db 验收 241 行收盘后样本，不联网。",
    )
    eastmoney_demo.add_argument(
        "--fixture-file",
        type=Path,
        default=Path(
            "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"
        ),
        help="已保存的真实公开分钟响应；离线读取，来源标为离线回放。",
    )
    probe_fixture_demo = commands.add_parser(
        "eastmoney-probe-fixture-demo",
        help="用保存样本离线验收双探针；独立嵌目录新库，不联网、不实际等待。",
    )
    probe_fixture_demo.add_argument(
        "--fixture-file",
        type=Path,
        default=Path(
            "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"
        ),
        help="C2 保存的公开分钟响应；离线读取并切成两轮响应。",
    )
    live_minute = commands.add_parser(
        "akshare-live-minute",
        help="逐字确认后请求一次东方财富公开分钟，只写独立 akshare-live-test.db。",
    )
    live_minute.add_argument("--session-date", required=True, help="YYYY-MM-DD。")
    live_minute.add_argument("--code", required=True, help="当前 C2 专用配置只允许 000001。")
    live_minute.add_argument(
        "--confirmed-through",
        required=True,
        help="必须显式填写 none（全部未确认）或带 +08:00 的整分钟确认边界。",
    )
    live_minute.add_argument(
        "--confirmation",
        required=True,
        help="必须与文档给出的真实请求确认文本逐字一致。",
    )
    intraday_probe = commands.add_parser(
        "akshare-live-probe",
        help="逐字确认后执行两次盘中分钟探针，间隔 300 秒，只写独立测试库。",
    )
    intraday_probe.add_argument("--session-date", required=True, help="已核实的当日开市日期。")
    intraday_probe.add_argument("--code", required=True, help="当前专用配置只允许 000001。")
    intraday_probe.add_argument(
        "--confirmation", required=True,
        help="必须与双探针文档确认文本逐字一致；固定两次请求，无自动重试。",
    )

    monitor_demo = commands.add_parser(
        "monitor-demo", help="用模拟时钟验收暂停、断网、午休和收盘，不实际等待。"
    )
    monitor_demo.add_argument("--session-date", default="2026-01-06", help="模拟交易日。")
    commands.add_parser("monitor-status", help="查看调度状态、连续检查点和恢复事件。")

    analysis_demo = commands.add_parser(
        "analysis-demo", help="在独立 analysis-test.db 验收指标、规则、止损和 MA20。"
    )
    analysis_demo.add_argument("--session-date", default="2026-01-07", help="模拟交易日，周一至周五。")
    replay_analysis = commands.add_parser(
        "analyze-replay", help="对已保存分钟线生成一次回放快照和补记，不发实时提醒。"
    )
    replay_analysis.add_argument("--code", required=True)
    replay_analysis.add_argument("--target-time", required=True, help="带 +08:00 的交易分钟。")
    daily_analysis = commands.add_parser(
        "daily-analyze", help="只用已确认前复权日 K 重算 MA20，结果标为回放补记。"
    )
    daily_analysis.add_argument("--through-date", required=True, help="YYYY-MM-DD。")
    daily_analysis.add_argument("--code", help="省略时分析配置中的全部自选股。")
    analysis_status = commands.add_parser("analysis-status", help="查看指标快照、规则事件和日线结论。")
    analysis_status.add_argument("--session-date", help="默认使用最近有分析记录的日期。")
    trace = commands.add_parser("analysis-trace", help="从规则事件追溯检查、快照和原始行情 ID。")
    trace.add_argument("--event-id", required=True, type=int)
    review_daily = commands.add_parser(
        "review-daily",
        help="手动生成确定性本地每日复盘；只读上游证据，不调用 Ollama 或云端。",
    )
    review_daily.add_argument("--session-date", required=True, help="YYYY-MM-DD。")
    review_daily.add_argument("--code", help="省略时复盘配置中的全部自选股。")
    cloud_review = commands.add_parser(
        "cloud-review",
        help="对已有本地复盘显式执行一次云端请求；总开关、daily_review 白名单和确认缺一不可。",
    )
    cloud_review.add_argument("--review-id", required=True, type=int)
    cloud_review.add_argument(
        "--confirmation", required=True,
        help=f"必须逐字填写：{CLOUD_REVIEW_CONFIRMATION}",
    )
    review_status = commands.add_parser("review-status", help="只读查看本地复盘和云端调用审计。")
    review_status.add_argument("--review-id", type=int, help="省略时列出最近复盘。")
    summary_demo = commands.add_parser(
        "summary-demo", help="在独立数据库验收异步摘要；默认使用模拟模型响应。"
    )
    summary_demo.add_argument("--session-date", default="2026-01-08", help="模拟交易日。")
    summary_demo.add_argument(
        "--real-ollama", action="store_true",
        help="显式请求本机 Ollama；必须配合 configs/summary-live-test.yaml。",
    )
    commands.add_parser("summary-status", help="只读查看摘要任务、模板回退和模型调用计数。")
    summary_trace = commands.add_parser(
        "summary-trace", help="只读查看摘要任务的上游检查链和模型实测字段。"
    )
    summary_trace.add_argument("--task-id", required=True, type=int)
    return parser


def _database(config: AppConfig) -> Database:
    return Database(config.storage.database)


def _require_initialized(database: Database) -> None:
    if not database.is_initialized():
        raise ConfigError("数据库尚未初始化，请先运行：python -m pandeng.app init")


def _print_watchlist(config: AppConfig) -> None:
    print(f"自选股（{len(config.watchlist)} 只）：")
    for item in config.watchlist:
        display_name = item.name or "未命名"
        sector = f"；板块代码 {item.sector_code}" if item.sector_code else ""
        print(f"  {item.code}  {display_name}{sector}")


def _show_config(config: AppConfig) -> None:
    print("盘灯配置检查通过。")
    print(
        f"检查间隔：{config.monitor.interval_seconds} 秒；"
        f"{polling_interval_label(config.monitor.interval_seconds)}"
    )
    print(
        "推荐轮询档位："
        + "、".join(f"{seconds} 秒={label}" for seconds, label in POLLING_PRESETS.items())
        + "；其他 60～3600 秒合法值按自定义档处理"
    )
    print(
        f"数据请求超时：{config.monitor.fetch_timeout_seconds} 秒；"
        f"收盘收尾上限：{config.monitor.close_timeout_seconds} 秒"
    )
    print(f"数据库：{config.storage.database}")
    print(
        f"规则：快速涨跌幅 ±{config.rules.rapid_move_pct}%；"
        f"放量阈值 {config.rules.volume_ratio_threshold} 倍；"
        f"普通冷却 {config.rules.normal_cooldown_seconds} 秒；"
        f"高优先级重复提醒 {config.rules.high_priority_repeat_seconds} 秒"
    )
    print(
        f"摘要：队列容量 {config.summary.queue_capacity}；"
        f"收尾等待上限 {config.summary.shutdown_timeout_seconds} 秒；"
        f"最终摘要最多 {config.summary.max_chars} 字符（无最低长度要求）"
    )
    _print_watchlist(config)
    print(
        f"Ollama：{'启用' if config.ollama.enabled else '关闭'}；"
        f"模型={config.ollama.model}；地址={config.ollama.base_url}；"
        f"超时={config.ollama.timeout_seconds} 秒"
    )
    key_status = "已配置" if config.cloud.api_key_configured else "未配置"
    print(
        f"云端：{'启用' if config.cloud.enabled else '关闭'}；"
        f"API Key={key_status}；白名单事件数={len(config.cloud.event_whitelist)}"
    )


def _status(config: AppConfig) -> None:
    database = _database(config)
    print("盘灯当前状态")
    print(f"数据库路径：{database.path}")
    print(f"数据库已初始化：{'是' if database.is_initialized() else '否'}")
    print(
        f"行情检查间隔：{config.monitor.interval_seconds} 秒；"
        f"{polling_interval_label(config.monitor.interval_seconds)}"
    )
    _print_watchlist(config)
    print(f"Ollama 开关：{'启用' if config.ollama.enabled else '关闭'}")
    print(f"云端开关：{'启用' if config.cloud.enabled else '关闭'}")
    if database.is_initialized():
        print(f"数据库结构版本：{database.metadata('schema_version')}")
        print(f"真实持仓批次数：{len(database.list_position_lots())}")
    print(
        "当前为手动复盘与云端门控 C4：新增 VWAP 摘要四位小数、分钟确认单向推进、"
        "确定性本地每日复盘，以及显式单次云端请求与审计。"
        "本地复盘不会调用 Ollama 或云端；真实分钟仍未接入自动轮询，"
        "真实前复权日线和可靠交易日历仍未接入。"
        "status 本身不启动轮询或调用模型，也不请求行情。"
    )


def _list_holdings(config: AppConfig) -> None:
    database = _database(config)
    _require_initialized(database)
    lots = database.list_position_lots()
    if not lots:
        print("当前没有已确认的真实持仓批次。")
        print("新增必须使用 holdings add，并逐字填写人工确认文本；程序不会从行情或模型推断持仓。")
        return
    print("真实持仓批次（归档记录仍保留，不参与当前计算）：")
    for lot in lots:
        stop_loss = (
            "未设置" if lot["stop_loss_price"] is None else str(lot["stop_loss_price"])
        )
        state = "有效" if bool(lot["active"]) else f"已归档：{lot['deactivation_reason']}"
        print(
            f"  批次 {lot['id']}：{lot['stock_code']}，数量 {lot['quantity']}，"
            f"买入日期 {lot['buy_date']}，固定止损 {stop_loss}，{state}，"
            f"确认时间 {lot['confirmed_at']}"
        )


def _add_holding(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    lot_id = add_position_lot(
        database, stock_code=args.code, quantity=args.quantity, buy_date=args.buy_date,
        stop_loss_price=args.stop_loss, confirmation=args.confirmation,
    )
    print(f"已人工确认并新增持仓批次 {lot_id}，同时写入 position_audit。")
    print("程序不会自动修改该批次；如需更正，请归档原记录后另行新增。")


def _archive_holding(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    archive_position_lot(
        database, lot_id=args.lot_id, confirmation=args.confirmation, reason=args.reason,
    )
    print(f"持仓批次 {args.lot_id} 已归档，原始记录和审计历史均未删除。")


def _holding_t1(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    result = t1_availability(database, args.code, _date_argument(args.as_of_date))
    print(
        f"{result.stock_code} 截至 {result.as_of_date}：有效记录总量 {result.total_quantity}；"
        f"可卖数量 {result.sellable_quantity if result.sellable_quantity is not None else '未知'}；"
        f"状态 {result.status}。"
    )
    for lot in result.lots:
        sellable = lot.sellable_quantity if lot.sellable_quantity is not None else "未知"
        print(
            f"  批次 {lot.lot_id}：记录数量 {lot.total_quantity}，可卖 {sellable}，"
            f"状态 {lot.status}，首个开市日 {lot.next_open_date or '未知/尚未出现'}；{lot.reason}"
        )
    print("该结果只由人工批次和明确交易日历计算，不代表真实账户查询，也不执行交易。")


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("日期必须使用 YYYY-MM-DD 格式。") from exc


def _replay_clock(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("replay-clock 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError("replay-clock 必须带时区，例如 +08:00。")
    return parsed


def _print_summary(label: str, summary: IngestionSummary) -> None:
    print(
        f"{label}：批次 {summary.run_id}；收到 {summary.received}；"
        f"新增 {summary.accepted}；去重 {summary.duplicates}；"
        f"拒绝 {summary.rejected}；{summary.status_text}"
    )
    if summary.unknown_codes:
        print(f"  状态未知的代码：{', '.join(summary.unknown_codes)}")


def _simulate(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    session_date = _date_argument(args.session_date)
    if not 1 <= args.daily_days <= 60:
        raise ConfigError("daily-days 必须在 1～60 之间。")
    try:
        start, end = demo_time_range(session_date, args.minutes)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    codes = [item.code for item in config.watchlist]
    adapter = make_simulator(codes, session_date, args.daily_days)
    dates = adapter.dates
    minute_summary = ingest_minutes(
        database,
        adapter.fetch_minute_bars(codes, start, end),
        adapter=adapter.name,
        replay_clock=end,
    )
    daily_summary = ingest_daily(
        database,
        adapter.fetch_daily_qfq(codes, dates[0], session_date),
        adapter=adapter.name,
    )
    calendar_summary = ingest_calendar(
        database,
        adapter.fetch_trading_calendar(dates[0], session_date),
        adapter=adapter.name,
    )
    print("离线模拟处理完成，未访问网络或调用模型。")
    _print_summary("原始分钟行情", minute_summary)
    _print_summary("前复权日 K", daily_summary)
    _print_summary("模拟交易日历", calendar_summary)
    print("可运行 python -m pandeng.app data-status 查看保存结果。")


def _replay(config: AppConfig, args: argparse.Namespace) -> None:
    if not any((args.minute_file, args.daily_file, args.calendar_file)):
        raise ConfigError("replay 至少需要一个 --minute-file、--daily-file 或 --calendar-file。")
    database = _database(config)
    _require_initialized(database)
    adapter = CsvReplayAdapter(args.minute_file, args.daily_file, args.calendar_file)
    codes = [item.code for item in config.watchlist]
    start_date, end_date = date(2000, 1, 1), date(2100, 12, 31)
    # 显式使用 UTC+8，避免依赖电脑当前时区。
    start_time = datetime(2000, 1, 1, tzinfo=CHINA_TZ)
    end_time = datetime(2100, 12, 31, 23, 59, tzinfo=CHINA_TZ)
    replay_clock = _replay_clock(args.replay_clock)

    print("CSV 回放处理完成，未访问网络或调用模型。")
    if args.minute_file:
        _print_summary(
            "原始分钟行情",
            ingest_minutes(
                database,
                adapter.fetch_minute_bars(codes, start_time, end_time),
                adapter=adapter.name,
                replay_clock=replay_clock,
            ),
        )
    if args.daily_file:
        _print_summary(
            "前复权日 K",
            ingest_daily(
                database,
                adapter.fetch_daily_qfq(codes, start_date, end_date),
                adapter=adapter.name,
            ),
        )
    if args.calendar_file:
        _print_summary(
            "交易日历",
            ingest_calendar(
                database,
                adapter.fetch_trading_calendar(start_date, end_date),
                adapter=adapter.name,
            ),
        )


def _data_status(config: AppConfig) -> None:
    database = _database(config)
    _require_initialized(database)
    print("盘灯数据状态")
    print(f"分钟记录：{table_count(database, 'minute_bars')}")
    print(f"前复权日线：{table_count(database, 'daily_qfq_bars')}")
    print(f"交易日历：{table_count(database, 'trading_calendar')}")
    print(f"质量与去重记录：{table_count(database, 'data_quality_issues')}")
    latest = latest_minutes(database)
    if latest:
        print("每只股票最新已保存的分钟行情：")
        for row in latest:
            print(
                f"  {row['stock_code']}  {row['bar_time']}  "
                f"收盘 {row['close']:.2f}  成交量 {row['volume']}  来源 {row['source']}"
            )
    runs = recent_runs(database)
    if runs:
        print("最近处理批次（新到旧）：")
        for row in runs:
            print(
                f"  #{row['id']} {row['data_kind']} {row['status']} "
                f"收到 {row['received_count']} 新增 {row['accepted_count']} "
                f"去重 {row['duplicate_count']} 拒绝 {row['rejected_count']}"
            )

def _data_audit(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    session_date = _date_argument(args.session_date)
    configured = tuple(item.code for item in config.watchlist)
    if args.code is not None and args.code not in configured:
        raise ConfigError("--code 必须属于当前 YAML 的观察对象列表。")
    codes = (args.code,) if args.code is not None else configured
    report = audit_market_data(
        database, codes, session_date, _target_time(args.as_of_time)
    )
    _print_audit_report(report)
    print("本次体检只读 SQLite，没有补数、改写结论、请求行情或调用模型。")


def _print_audit_report(report: AuditReport) -> None:
    calendar_text = {
        "open": "明确开市", "closed": "明确休市", "unknown": "状态未知"
    }[report.calendar_status]
    print(
        f"盘灯只读数据体检：{report.session_date.isoformat()}；"
        f"截至 {report.as_of_time.isoformat()}；日历 {calendar_text}。"
    )
    print(
        f"分钟总体状态 {report.status}；完整 {report.complete_count} 只；"
        f"仅价格线可用 {report.price_only_count} 只；状态未知 {report.unknown_count} 只。"
    )
    status_text = {"ok": "完整", "price_only": "仅价格线可用", "unknown": "状态未知"}
    daily_text = {
        "confirmed": "已确认", "unconfirmed": "未确认",
        "missing": "缺失", "unknown": "状态未知",
    }
    for item in report.codes:
        print(
            f"  {item.stock_code}：{status_text[item.status]}；"
            f"分钟已保存 {item.saved_minutes}/{item.expected_minutes}；"
            f"已确认 {item.confirmed_minutes}；未确认 {item.unconfirmed_minutes}；"
            f"缺口 {len(item.missing_times)}；质量错误 {item.quality_error_count}。"
        )
        if item.missing_times:
            preview = ", ".join(item.missing_times[:3])
            suffix = "……" if len(item.missing_times) > 3 else ""
            print(f"    缺口示例：{preview}{suffix}")
        target_state = (
            "缺失" if item.target_confirmed is None
            else "已确认" if item.target_confirmed else "未确认"
        )
        print(
            f"    目标分钟 {target_state}；"
            f"价格线 {'可用' if item.price_line_available else '未知'}；"
            f"最新分钟 {item.latest_minute_time or '无'}；"
            f"来源 {', '.join(item.sources) or '无'}。"
        )
        print(
            f"    连续检查点 {item.checkpoint_time or '无'}；"
            f"调度状态 {item.checkpoint_status or '无'}；"
            f"当日前复权日 K {daily_text[item.daily_status]}。"
        )
        for reason in item.reasons:
            print(f"    说明：{reason}")
    print("日 K 状态单独报告；分钟体检通过不会把收盘或日 K 确认混为一谈。")
    print("本报告按当前数据库回看，不证明这些数据在当时已经实时到达。")


def _adapter_demo(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    outcome = run_adapter_demo(
        config, _date_argument(args.session_date), args.feed_file.expanduser().resolve()
    )
    print(
        f"验收记录汇总：分钟 {outcome.minute_rows} 条；前复权日线 {outcome.daily_rows} 条；"
        f"交易日历 {outcome.calendar_rows} 条；只读分钟请求 {len(outcome.requests)} 次。"
    )
    print(
        f"可运行 python -m pandeng.app --config {args.config} monitor-status "
        "查看增量检查点。"
    )


def _provider_fixture_demo(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    outcome = run_provider_fixture_demo(
        config,
        _date_argument(args.session_date),
        args.fixture_file.expanduser().resolve(),
    )
    print(
        f"C1 验收汇总：分钟 {outcome.minute_rows} 条；前复权日线 {outcome.daily_rows} 条；"
        f"交易日历 {outcome.calendar_rows} 条；09:30 排除 "
        f"{outcome.excluded_opening_auction} 条。"
    )
    print(
        f"可运行 python -m pandeng.app --config {args.config} data-audit "
        f"--session-date {args.session_date} "
        f"--as-of-time {args.session_date}T09:32:00+08:00 查看只读体检。"
    )


def _confirmed_through_argument(value: str) -> datetime | None:
    if value.strip().lower() == "none":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("confirmed-through 必须填写 none 或 ISO 8601 整分钟。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError("confirmed-through 必须带时区，例如 +08:00；或明确填写 none。")
    if parsed.second or parsed.microsecond:
        raise ConfigError("confirmed-through 必须是整分钟。")
    return parsed.astimezone(CHINA_TZ)


def _eastmoney_fixture_demo(config: AppConfig, args: argparse.Namespace) -> None:
    outcome = run_eastmoney_fixture_demo(
        config, args.fixture_file.expanduser().resolve()
    )
    print(
        f"C2 离线验收汇总：原始 {outcome.raw_rows} 行；常规分钟 {outcome.regular_rows} 条；"
        f"09:30 排除 {outcome.excluded_0930} 条；零成交量保留 "
        f"{outcome.zero_volume_rows} 条。"
    )


def _eastmoney_intraday_fixture_demo(config: AppConfig, args: argparse.Namespace) -> None:
    outcome = run_eastmoney_intraday_fixture_demo(
        config, args.fixture_file.expanduser().resolve()
    )
    print(
        f"C3 离线双探针汇总：模拟请求 {outcome.request_count} 次；"
        f"重叠一致 {outcome.comparison.unchanged_count}；"
        f"新增 {len(outcome.comparison.new_in_second)}；审计 {outcome.audit_path}。"
    )


def _akshare_live_minute(config: AppConfig, args: argparse.Namespace) -> None:
    outcome = run_akshare_live_minute(
        config,
        _date_argument(args.session_date),
        args.code,
        confirmation=args.confirmation,
        confirmed_through=_confirmed_through_argument(args.confirmed_through),
    )
    print(
        f"C2 真实请求汇总：实际请求 {outcome.request_count} 次；"
        f"常规分钟 {len(outcome.mapping.records)} 条；审计文件 {outcome.audit_path}。"
    )
    if outcome.ingestion.status != "ok":
        raise ConfigError("真实分钟已留审计，但入库质量状态未知；请停止后续分析并查看体检。")


def _akshare_intraday_probe(config: AppConfig, args: argparse.Namespace) -> None:
    outcome = run_akshare_intraday_probe(
        config,
        _date_argument(args.session_date),
        args.code,
        confirmation=args.confirmation,
    )
    print(
        f"C3 双探针汇总：实际请求 {outcome.request_count} 次；"
        f"重叠 {outcome.comparison.overlap_count}；变化 {len(outcome.comparison.changed_times)}；"
        f"汇总审计 {outcome.audit_path}。"
    )
    if outcome.first.ingestion.status != "ok" or outcome.second.ingestion.status != "ok":
        print("警告：至少一次入库存在质量异常；请停止后续分析并查看审计与只读体检。")


def _monitor_demo(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    session_date = _date_argument(args.session_date)
    outcome = run_monitor_demo(config, session_date)
    print(
        f"验收记录汇总：离线恢复事件 {outcome.offline_recovery_events} 条；"
        f"日 K 状态 {outcome.daily_status}。"
    )
    print(
        f"可运行 python -m pandeng.app --config {args.config} monitor-status "
        "查看检查点和事件。"
    )


def _monitor_status(config: AppConfig) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    state = get_monitor_state(database)
    print("盘灯调度状态")
    if state is None:
        print("尚未运行 monitor-demo，没有调度状态。")
        return
    print(
        f"交易日 {state['session_date']}；运行状态 {state['status']}；"
        f"日历 {state['calendar_status']}；日 K {state['daily_status']}；"
        f"停止原因 {state['stop_reason'] or '无'}"
    )
    print(f"最近轮次：{state['last_round_at'] or '无'}")
    print(f"下一轮时间：{state['next_poll_at'] or '无（暂停或已结束）'}")
    states = get_code_states(database, state["session_date"])
    if states:
        print("逐股票连续检查点：")
        for code, row in sorted(states.items()):
            print(
                f"  {code}：{row['status']}；连续到 {row['last_contiguous_time'] or '无'}；"
                f"原因 {row['unknown_reason'] or '无'}"
            )
    rounds = recent_monitor_rounds(database, limit=8)
    if rounds:
        print("最近调度轮次（新到旧）：")
        for row in rounds:
            print(
                f"  #{row['id']} {row['scheduled_at']} {row['trigger']} "
                f"恢复={bool(row['recovery'])} {row['status']} 新增 {row['accepted_count']} "
                f"可分析 {row['analysis_ready_count']}"
            )
    events = recent_monitor_events(database, limit=8)
    if events:
        print("最近调度事件（新到旧）：")
        for row in events:
            code = row["stock_code"] or "全局"
            print(f"  #{row['id']} {code} {row['event_type']}：{row['message']}")


def _analysis_service(config: AppConfig, database: Database) -> AnalysisService:
    sectors = {item.code: item.sector_code for item in config.watchlist if item.sector_code}
    return AnalysisService(database, rules=config.rules, sector_map=sectors)


def _target_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("target-time 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError("target-time 必须带时区，例如 2026-01-05T09:35:00+08:00。")
    return parsed.astimezone(CHINA_TZ)


def _print_snapshot(database: Database, result: AnalysisResult) -> None:
    print(
        f"回放分析：运行 {result.analysis_run_id}，快照 {result.snapshot_id}，"
        f"{result.stock_code} {result.target_time.isoformat()}，状态 {result.status}，"
        f"{'已存在并跳过重复执行' if result.skipped else '本次已保存'}。"
    )
    if result.snapshot_id is None:
        return
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM indicator_snapshots WHERE id = ?", (result.snapshot_id,)
        ).fetchone()
    if row is None:
        print("快照记录缺失，状态未知。")
        return
    values = json.loads(row["values_json"])
    reasons = json.loads(row["unknown_reasons_json"])
    print(
        f"  收盘 {values.get('close')}；1 分钟涨跌幅 {values.get('return_1m_pct')}；"
        f"5 分钟涨跌幅 {values.get('return_5m_pct')}；成交量比 {values.get('volume_ratio')}。"
    )
    print(
        f"  典型价估算 VWAP {values.get('vwap_estimate')}；"
        f"日内高/低 {values.get('day_high')}/{values.get('day_low')}；"
        f"MA5/分钟 MA20 {values.get('ma5')}/{values.get('ma20_minute')}；"
        f"相对板块 {values.get('relative_sector_pct')}。"
    )
    for reason in reasons:
        print(f"  字段未知：{reason}")
    print("回放与恢复模式只保存补记，不产生实时提醒，也不调用本地或云端模型。")


def _analyze_replay(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    result = _analysis_service(config, database).analyze_code(
        args.code, _target_time(args.target_time), analysis_mode="replay"
    )
    _print_snapshot(database, result)


def _daily_analyze(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    through = _date_argument(args.through_date)
    codes = [args.code] if args.code else [item.code for item in config.watchlist]
    for code in codes:
        results = analyze_daily(database, code, through, analysis_mode="replay")
        if not results:
            print(f"{code}：截至 {through.isoformat()} 没有前复权日 K，状态未知。")
            continue
        result = results[-1]
        signal = "未知" if result.formal_signal is None else "是" if result.formal_signal else "否"
        ma20 = "未知" if result.ma20 is None else f"{result.ma20:.6f}"
        print(
            f"{code} {result.trade_date}：状态 {result.status}；MA20 {ma20}；"
            f"拐头天数 {result.turn_days if result.turn_days is not None else '未知'}；"
            f"正式双条件 {signal}；回放补记={'是' if result.is_backfill else '否'}。"
        )
        if result.reason:
            print(f"  原因：{result.reason}")
    print("日线结果只使用 confirmed=true 的前复权日 K；本命令全部按回放补记处理。")


def _analysis_status(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    session_date = args.session_date
    with database.connect() as connection:
        if session_date is None:
            row = connection.execute("SELECT MAX(session_date) AS value FROM analysis_runs").fetchone()
            session_date = row["value"]
        if not session_date:
            print("尚未有分析运行。可先运行 analyze-replay 或独立 analysis-demo。")
            return
        _date_argument(session_date)
        print(f"盘灯分析状态：{session_date}")
        snapshots = list(connection.execute(
            """
            SELECT * FROM indicator_snapshots
            WHERE session_date = ? ORDER BY id DESC LIMIT 20
            """,
            (session_date,),
        ))
        runs = list(connection.execute(
            "SELECT * FROM analysis_runs WHERE session_date = ? ORDER BY id DESC LIMIT 20",
            (session_date,),
        ))
        events = list(connection.execute(
            "SELECT * FROM rule_events WHERE session_date = ? ORDER BY id DESC LIMIT 20",
            (session_date,),
        ))
        daily = list(connection.execute(
            "SELECT * FROM daily_analysis WHERE trade_date = ? ORDER BY stock_code",
            (session_date,),
        ))
        recovery = connection.execute(
            """
            SELECT COUNT(*) AS checks, COALESCE(SUM(c.notified), 0) AS notices
            FROM rule_event_checks AS c
            JOIN analysis_runs AS r ON r.id = c.analysis_run_id
            WHERE r.session_date = ? AND r.analysis_mode = 'offline_recovery'
            """,
            (session_date,),
        ).fetchone()
    print(f"最近分析运行 {len(runs)} 条；指标快照 {len(snapshots)} 条（最多显示 20 条）。")
    for row in snapshots:
        values = json.loads(row["values_json"])
        print(
            f"  快照 {row['id']} {row['stock_code']} {row['data_time']} {row['status']}："
            f"收盘 {values.get('close')}，VWAP 估算 {values.get('vwap_estimate')}，"
            f"成交量比 {values.get('volume_ratio')}，来源分钟数 "
            f"{len(json.loads(row['source_bar_ids_json']))}。"
        )
    if events:
        print("规则事件（新到旧）：")
        for row in events:
            print(
                f"  事件 {row['id']} {row['stock_code']} {row['event_type']} "
                f"{row['priority']} {row['status']}；检查 {row['check_count']}；"
                f"提醒 {row['reminder_count']}；批次 {row['position_lot_id'] or '无'}。"
            )
    print(
        f"离线恢复检查 {recovery['checks']} 条；其中实时提醒 {recovery['notices']} 条"
        "（安全口径要求为 0）。"
    )
    if daily:
        print("日线分析：")
        for row in daily:
            signal = "未知" if row["formal_signal"] is None else "是" if row["formal_signal"] else "否"
            print(
                f"  {row['stock_code']} MA20 {row['ma20']}；拐头 {row['turn_days']}；"
                f"双条件 {signal}；模式 {row['analysis_mode']}；补记={bool(row['is_backfill'])}。"
            )


def _analysis_trace(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    with database.connect() as connection:
        event = connection.execute("SELECT * FROM rule_events WHERE id = ?", (args.event_id,)).fetchone()
        if event is None:
            raise ConfigError(f"规则事件 {args.event_id} 不存在。")
        check = connection.execute(
            "SELECT * FROM rule_event_checks WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (args.event_id,),
        ).fetchone()
        snapshot = None if check is None else connection.execute(
            "SELECT * FROM indicator_snapshots WHERE id = ?", (check["snapshot_id"],)
        ).fetchone()
        run = None if check is None else connection.execute(
            "SELECT * FROM analysis_runs WHERE id = ?", (check["analysis_run_id"],)
        ).fetchone()
    print(
        f"规则事件 {event['id']}：{event['stock_code']} {event['event_type']}；"
        f"指纹 {event['fingerprint']}；状态 {event['status']}。"
    )
    if check is None or snapshot is None or run is None:
        print("该事件尚无完整检查链，状态未知。")
        return
    print(
        f"检查 {check['id']}：active={check['is_active']}（空值表示未知）；"
        f"notified={check['notified']}；类型 {check['notification_kind']}。"
    )
    print(
        f"指标快照 {snapshot['id']}：状态 {snapshot['status']}；"
        f"最新分钟原始 ID {snapshot['latest_minute_bar_id']}；"
        f"全部来源分钟 ID {snapshot['source_bar_ids_json']}。"
    )
    print(
        f"分析运行 {run['id']}：模式 {run['analysis_mode']}；"
        f"调度轮次 {run['monitor_round_id']}；输入 SHA-256 {run['input_hash']}。"
    )
    print("追溯链为：原始行情 ID → 指标快照 ID → 规则事件 ID → 事件检查 ID。")


def _review_daily(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到结构版本 6。")
    configured = tuple(item.code for item in config.watchlist)
    if args.code is not None and args.code not in configured:
        raise ConfigError("--code 必须属于当前 YAML 的观察对象列表。")
    codes = (args.code,) if args.code is not None else configured
    session_date = _date_argument(args.session_date)
    for code in codes:
        outcome = create_local_review(
            database, session_date, code, momentum=config.momentum
        )
        print(outcome.report)
        print(
            f"本地复盘编号 {outcome.review_id}；"
            f"{'本次新建' if outcome.created else '相同输入已存在，幂等返回'}；"
            f"输入 SHA-256 {outcome.input_hash}；本次云端调用 0 次。"
        )
    print("如需云端整理，必须另行运行 cloud-review 并满足全部显式门控。")


def _cloud_review(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到结构版本 6。")
    outcome = CloudReviewService(config, database).run(
        args.review_id, confirmation=args.confirmation
    )
    if outcome.status != "succeeded":
        raise ConfigError(
            f"云端调用审计 {outcome.call_id} 已保存为失败；类型 {outcome.error_type}；"
            f"{outcome.error_message}。不会自动重试。"
        )
    print(
        f"云端复盘调用 {outcome.call_id} 成功；本地复盘 {outcome.review_id}；"
        "实际请求 1 次；自动重试 0 次。"
    )
    print(outcome.result_text or "")
    print("云端文字仅作为研究记录保存，未修改本地行情、指标、规则结论或持仓。")


def _review_status(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到结构版本 6。")
    if args.review_id is None:
        rows = recent_reviews(database)
        print("盘灯本地复盘记录（新到旧）：")
        if not rows:
            print("尚无复盘；请先手动运行 review-daily。")
            return
        for row in rows:
            print(
                f"  复盘 {row['id']} {row['session_date']} {row['stock_code']}："
                f"数据状态 {row['data_status']}；云端 {row['cloud_status'] or '未调用'}；"
                f"输入 SHA-256 {row['input_hash']}。"
            )
        print("本命令只读，不会生成复盘或调用模型。")
        return
    review = load_review(database, args.review_id)
    if review is None:
        raise ConfigError(f"本地复盘 {args.review_id} 不存在。")
    print(review["local_report"])
    print(
        f"复盘 {review['id']}；数据状态 {review['data_status']}；"
        f"创建时间 {review['created_at']}；输入 SHA-256 {review['input_hash']}。"
    )
    with database.connect() as connection:
        call = connection.execute(
            "SELECT * FROM cloud_review_calls WHERE review_id = ?", (args.review_id,)
        ).fetchone()
    if call is None:
        print("云端：未调用。")
    else:
        print(
            f"云端调用 {call['id']}：状态 {call['status']}；模型 {call['model_name']}；"
            f"错误 {call['error_type'] or '无'}；耗时 {call['elapsed_ms']} ms；"
            f"token {call['total_tokens'] if call['total_tokens'] is not None else '未知'}。"
        )
        if call["result_text"]:
            print("云端研究记录：")
            print(call["result_text"])
        if call["error_message"]:
            print(f"云端错误：{call['error_message']}")
    print("审计不保存 API Key；本命令没有发起新请求。")


def _summary_demo(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    run_summary_demo(
        config, _date_argument(args.session_date), real_ollama=bool(args.real_ollama)
    )
    print(
        f"可运行 python -m pandeng.app --config {args.config} summary-status "
        "查看任务和模板回退。"
    )


def _summary_status(config: AppConfig) -> None:
    database = _database(config)
    _require_initialized(database)
    store = SummaryStore(database)
    counts = store.counts()
    rows = store.recent()
    print("盘灯摘要状态")
    print(
        f"等待 {counts['pending']}；运行 {counts['running']}；"
        f"模型成功 {counts['succeeded']}；模板回退 {counts['fallback']}；"
        f"模型调用 {counts['model_calls']}。"
    )
    if not rows:
        print("尚无摘要任务。离线验收请使用 configs/summary-test.yaml。")
        return
    for row in rows:
        print(
            f"  任务 {row['id']} {row['stock_code']} {row['event_type']}："
            f"{row['status']}；尝试 {row['attempt_count']}；恢复 {row['recovery_count']}；"
            f"结果 {row['summary_kind'] or '尚未生成'}。"
        )
        if row["summary_text"]:
            print(f"    {row['summary_text']}")
        if row["fallback_reason"]:
            print(f"    回退原因：{row['fallback_reason']}")


def _summary_trace(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    store = SummaryStore(database)
    task = store.task(args.task_id)
    if task is None:
        raise ConfigError(f"摘要任务 {args.task_id} 不存在。")
    facts = json.loads(task["input_json"])
    with database.connect() as connection:
        result = connection.execute(
            "SELECT * FROM summary_results WHERE task_id = ?", (args.task_id,)
        ).fetchone()
    print(
        f"摘要任务 {task['id']}：状态 {task['status']}；"
        f"事件检查 {facts['event_check_id']}；事件 {facts['event_id']}；"
        f"指标快照 {facts['snapshot_id']}；分析运行 {facts['analysis_run_id']}。"
    )
    print(f"回退原因：{task['fallback_reason'] or '无'}")
    if result is not None:
        print(f"最终保存类型：{result['summary_kind']}；内容：{result['text']}")
        print(f"校验记录：{result['validation_reasons_json']}")
    calls = store.calls_for_task(args.task_id)
    if not calls:
        print("没有模型调用记录；队列满或模型关闭时可以在零请求情况下回退。")
    for call in calls:
        metrics = {
            key: call[key] if call[key] is not None else "未知"
            for key in (
                "elapsed_ms", "total_duration_ns", "load_duration_ns",
                "eval_count", "eval_duration_ns",
            )
        }
        print(
            f"  调用 {call['id']}：{call['provider']} {call['model_name']}；"
            f"状态 {call['status']}；{metrics}。"
        )
        print(f"  错误：{call['error_message'] or '无'}")
        thinking = call["thinking_text"] or ""
        print(f"  R1 思考文本已单独审计，长度 {len(thinking)} 字符，不混入最终摘要。")


def _analysis_demo(config: AppConfig, args: argparse.Namespace) -> None:
    database = _database(config)
    _require_initialized(database)
    if database.metadata("schema_version") != "6":
        raise ConfigError("请先运行 python -m pandeng.app init，升级到当前结构。")
    run_analysis_demo(config, _date_argument(args.session_date))
    print(
        f"可运行 python -m pandeng.app --config {args.config} analysis-status "
        "查看快照、事件和恢复提醒计数。"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
        if args.command == "init":
            database = _database(config)
            database.initialize()
            print("盘灯数据库初始化/升级完成。")
            print(f"数据库：{database.path}")
            print("下一步可运行：python -m pandeng.app status")
        elif args.command == "show-config":
            _show_config(config)
        elif args.command == "status":
            _status(config)
        elif args.command == "holdings":
            if args.holdings_command == "list":
                _list_holdings(config)
            elif args.holdings_command == "add":
                _add_holding(config, args)
            elif args.holdings_command == "archive":
                _archive_holding(config, args)
            elif args.holdings_command == "t1":
                _holding_t1(config, args)
        elif args.command == "simulate":
            _simulate(config, args)
        elif args.command == "replay":
            _replay(config, args)
        elif args.command == "data-status":
            _data_status(config)
        elif args.command == "data-audit":
            _data_audit(config, args)
        elif args.command == "adapter-demo":
            _adapter_demo(config, args)
        elif args.command == "provider-fixture-demo":
            _provider_fixture_demo(config, args)
        elif args.command == "eastmoney-fixture-demo":
            _eastmoney_fixture_demo(config, args)
        elif args.command == "eastmoney-probe-fixture-demo":
            _eastmoney_intraday_fixture_demo(config, args)
        elif args.command == "akshare-live-minute":
            _akshare_live_minute(config, args)
        elif args.command == "akshare-live-probe":
            _akshare_intraday_probe(config, args)
        elif args.command == "monitor-demo":
            _monitor_demo(config, args)
        elif args.command == "monitor-status":
            _monitor_status(config)
        elif args.command == "summary-demo":
            _summary_demo(config, args)
        elif args.command == "summary-status":
            _summary_status(config)
        elif args.command == "summary-trace":
            _summary_trace(config, args)
        elif args.command == "analysis-demo":
            _analysis_demo(config, args)
        elif args.command == "analyze-replay":
            _analyze_replay(config, args)
        elif args.command == "daily-analyze":
            _daily_analyze(config, args)
        elif args.command == "analysis-status":
            _analysis_status(config, args)
        elif args.command == "analysis-trace":
            _analysis_trace(config, args)
        elif args.command == "review-daily":
            _review_daily(config, args)
        elif args.command == "cloud-review":
            _cloud_review(config, args)
        elif args.command == "review-status":
            _review_status(config, args)
        return 0
    except KeyboardInterrupt:
        print("已收到 Ctrl+C，盘灯停止运行。")
        return 130
    except (ConfigError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"错误：{exc}")
        return 1
