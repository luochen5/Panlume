"""C3 明日盘中双探针：两次请求、五分钟间隔、重叠响应比较。

本模块不判断真实节假日，也不接入自动轮询。只有调用方逐字确认后才会请求；
两次均显式保持 confirmed=false，任一请求失败都不会自动重试。
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.monitor.clock import Clock, ManualClock, SystemClock
from pandeng.monitor.schedule import session_target
from pandeng.storage.db import Database

from .akshare_demo import (
    LIVE_CONFIRMATION,
    EastmoneyLiveOutcome,
    _json_safe,
    _rows_hash,
    _write_audit,
    _require_fresh,
    _require_models_closed,
    _require_current_schema,
    run_akshare_live_minute,
)
from .provider_common import parse_china_provider_time
from .eastmoney_adapter import EastmoneyMinuteFixtureAdapter, LIVE_SOURCE
from .schedule import CHINA_TZ


PROBE_CONFIRMATION = "确认执行两次东方财富盘中公开分钟探针并写入独立测试库"
PROBE_INTERVAL_SECONDS = 300
PROBE_FORMAT = "pandeng.eastmoney-intraday-probe.v1"
PROBE_FIXTURE_SOURCE = "eastmoney_intraday_probe_fixture_offline"


@dataclass(frozen=True)
class ProbeComparison:
    status: str
    overlap_count: int
    unchanged_count: int
    changed_times: tuple[str, ...]
    missing_in_second: tuple[str, ...]
    new_in_second: tuple[str, ...]


@dataclass(frozen=True)
class IntradayProbeOutcome:
    first: EastmoneyLiveOutcome
    second: EastmoneyLiveOutcome
    comparison: ProbeComparison
    audit_path: Path
    request_count: int
    actual_interval_seconds: float


def _read_audit(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _actual_request_count(path: Path) -> int:
    value = _read_audit(path).get("request_count_actual", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _raw_rows_by_time(path: Path) -> dict[str, tuple[str, dict[str, object]]]:
    rows = _read_audit(path).get("raw_rows", [])
    if not isinstance(rows, list):
        raise ConfigError(f"请求审计中的 raw_rows 无效：{path}")
    indexed: dict[str, tuple[str, dict[str, object]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError(f"请求审计包含非对象原始行：{path}")
        bar_time = parse_china_provider_time(row.get("时间")).isoformat()
        if bar_time in indexed:
            raise ConfigError(f"请求审计存在重复时间，无法比较：{bar_time}")
        normalized = _json_safe(row)
        if not isinstance(normalized, dict):
            raise ConfigError("原始行规范化失败。")
        indexed[bar_time] = (_rows_hash([row]), normalized)
    return indexed


def compare_probe_responses(first_path: Path, second_path: Path) -> ProbeComparison:
    """比较规范化原始行；只报告证据，不修改分钟或确认状态。"""
    first = _raw_rows_by_time(first_path)
    second = _raw_rows_by_time(second_path)
    common = sorted(first.keys() & second.keys())
    changed = tuple(
        moment for moment in common if first[moment][0] != second[moment][0]
    )
    missing = tuple(sorted(first.keys() - second.keys()))
    added = tuple(sorted(second.keys() - first.keys()))
    if not common:
        status = "no_overlap"
    elif changed or missing:
        status = "changed"
    else:
        status = "unchanged"
    return ProbeComparison(
        status=status,
        overlap_count=len(common),
        unchanged_count=len(common) - len(changed),
        changed_times=changed,
        missing_in_second=missing,
        new_in_second=added,
    )


def _preflight(
    config: AppConfig, stock_code: str
) -> Database:
    database = Database(config.storage.database)
    if database.path.name != "akshare-live-test.db":
        raise ConfigError("盘中双探针只允许写入 akshare-live-test.db，已拒绝其他数据库。")
    _require_models_closed(config)
    _require_current_schema(database)
    if tuple(item.code for item in config.watchlist) != (stock_code,):
        raise ConfigError("双探针配置的观察对象必须与命令中的单个代码完全一致。")
    return database


def _probe_moments(
    now: datetime, session_date: date
) -> tuple[datetime, datetime, datetime, datetime]:
    local_now = now.astimezone(CHINA_TZ)
    later = local_now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
    first_target = session_target(local_now)
    second_target = session_target(later)
    if local_now.date() != session_date:
        raise ConfigError("session-date 必须与固定 UTC+8 下的当天日期一致。")
    if first_target is None or second_target is None:
        raise ConfigError(
            "双探针要求当前和五分钟后都处于交易分钟；午休、开盘前和收盘边界不会启动第一次请求。"
        )
    if first_target.date() != session_date or second_target.date() != session_date:
        raise ConfigError("双探针不能跨日期执行。")
    return local_now, later, first_target, second_target


def run_akshare_intraday_probe(
    config: AppConfig,
    session_date: date,
    stock_code: str,
    *,
    confirmation: str,
    fetcher: Callable[..., object] | None = None,
    clock: Clock | None = None,
    source: str = LIVE_SOURCE,
    offline: bool = False,
) -> IntradayProbeOutcome:
    """真实默认运行两次；本地响应、模拟时钟和离线模式仅供受控测试。"""
    if confirmation != PROBE_CONFIRMATION:
        raise ConfigError("双探针确认文本不匹配；没有发起请求、创建审计或写数据库。")
    if offline and (fetcher is None or clock is None or source == LIVE_SOURCE):
        raise ConfigError("离线双探针必须提供本地响应、模拟时钟和明确的离线来源。")
    if not offline and source != LIVE_SOURCE:
        raise ConfigError("真实双探针必须使用明确的实时来源。")
    database = _preflight(config, stock_code)
    clock = clock or SystemClock()
    started, scheduled_second, first_target, scheduled_second_target = _probe_moments(
        clock.now(), session_date
    )

    probe_id = uuid.uuid4().hex
    audit_root = database.path.parent / "akshare-live-audit"
    stem = f"eastmoney-probe-{stock_code}-{session_date.isoformat()}-{probe_id[:12]}"
    first_path = audit_root / f"{stem}-1.json"
    second_path = audit_root / f"{stem}-2.json"
    summary_path = audit_root / f"{stem}-summary.json"
    summary: dict[str, object] = {
        "format": PROBE_FORMAT,
        "probe_id": probe_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "planned",
        "request_mode": "offline_fixture" if offline else "live",
        "source": source,
        "session_date": session_date.isoformat(),
        "stock_code": stock_code,
        "database": str(database.path),
        "planned_request_count": 2,
        "request_count_actual": 0,
        "retry_count": 0,
        "interval_seconds_planned": PROBE_INTERVAL_SECONDS,
        "started_at": started.isoformat(),
        "second_scheduled_at": scheduled_second.isoformat(),
        "first_target": first_target.isoformat(),
        "second_target_scheduled": scheduled_second_target.isoformat(),
        "confirmed_through": None,
        "calendar_status": "not_checked_probe_only",
        "database_written": False,
        "model_called": False,
        "position_written": False,
        "request_audits": [str(first_path), str(second_path)],
        "completed_request_audits": [],
        "comparison": None,
    }
    _write_audit(summary_path, summary)
    if offline:
        print("盘灯 C3 离线双探针：模拟请求 2 次，模拟间隔 300 秒，不实际等待。")
        print("读取本地保存样本；真实网络请求 0 次，不导入 AKShare，不调用 Ollama 或云端。")
    else:
        print("盘灯 C3 盘中双探针：预计请求 2 次，间隔 300 秒，自动重试 0 次。")
        print("本命令不验证交易日历；只能在用户已核实的开市日、交易分钟内启动。")
    print(f"第一次目标 {first_target.isoformat()}；第二次预定目标 {scheduled_second_target.isoformat()}。")
    print("两次均 confirmed-through=none；不会自动确认最后一根或任何重叠分钟。")
    print(f"唯一写入数据库：{database.path}")
    print(f"双探针汇总审计：{summary_path}")

    completed: list[EastmoneyLiveOutcome] = []

    def refresh_status(status: str, error: BaseException | None = None) -> None:
        summary["status"] = status
        summary["request_count_actual"] = (
            _actual_request_count(first_path) + _actual_request_count(second_path)
        )
        summary["database_written"] = bool(completed)
        summary["completed_request_audits"] = [str(item.audit_path) for item in completed]
        if error is not None:
            summary["error_type"] = type(error).__name__
            summary["error_message"] = str(error)[:500]
        _write_audit(summary_path, summary)

    try:
        first = run_akshare_live_minute(
            config,
            session_date,
            stock_code,
            confirmation=LIVE_CONFIRMATION,
            confirmed_through=None,
            fetcher=fetcher,
            request_end=first_target,
            audit_path=first_path,
            source=source,
            offline=offline,
        )
        completed.append(first)
        refresh_status("first_complete_waiting")
        if offline:
            print("第一次模拟切片完成；模拟时钟推进 300 秒，不实际等待。")
        else:
            print(f"第一次完成；现在等待 {PROBE_INTERVAL_SECONDS} 秒。Ctrl+C 会保留已提交数据并停止。")
        clock.sleep(PROBE_INTERVAL_SECONDS)

        second_now = clock.now().astimezone(CHINA_TZ)
        second_target = session_target(second_now)
        if second_now.date() != session_date or second_target is None:
            raise ConfigError("等待结束后已不在当日交易分钟；第二次请求未执行，第一次结果已保留。")
        actual_interval = (second_now - started).total_seconds()
        summary["second_started_at"] = second_now.isoformat()
        summary["second_target_actual"] = second_target.isoformat()
        summary["actual_interval_seconds"] = actual_interval
        _write_audit(summary_path, summary)
        second = run_akshare_live_minute(
            config,
            session_date,
            stock_code,
            confirmation=LIVE_CONFIRMATION,
            confirmed_through=None,
            fetcher=fetcher,
            request_end=second_target,
            audit_path=second_path,
            source=source,
            offline=offline,
        )
        completed.append(second)
        comparison = compare_probe_responses(first.audit_path, second.audit_path)
        summary["comparison"] = asdict(comparison)
        ingestion_ok = first.ingestion.status == "ok" and second.ingestion.status == "ok"
        summary["ingestion_status"] = "ok" if ingestion_ok else "unknown"
        refresh_status("completed" if ingestion_ok else "completed_with_quality_errors")
    except KeyboardInterrupt as exc:
        refresh_status("interrupted", exc)
        print("双探针被用户中断；已提交数据和请求审计保留，不会自动补发第二次。")
        raise
    except Exception as exc:
        refresh_status("failed", exc)
        raise

    print(
        f"双探针完成：实际请求 {summary['request_count_actual']} 次；"
        f"实际间隔 {actual_interval:.0f} 秒；重叠 {comparison.overlap_count} 个时间点。"
    )
    print(
        f"重叠一致 {comparison.unchanged_count}；内容变化 {len(comparison.changed_times)}；"
        f"第二次消失 {len(comparison.missing_in_second)}；第二次新增 {len(comparison.new_in_second)}。"
    )
    if comparison.changed_times:
        print("变化时间点：" + ", ".join(comparison.changed_times[:5]))
    if comparison.missing_in_second:
        print("第二次消失时间点：" + ", ".join(comparison.missing_in_second[:5]))
    print(f"重叠比较状态：{comparison.status}。该结果只是证据，不会自动生成确认边界。")
    print("全部常规分钟仍未确认；主库、真实持仓、Ollama 和云端均未修改或调用。")
    return IntradayProbeOutcome(
        first,
        second,
        comparison,
        summary_path,
        int(summary["request_count_actual"]),
        actual_interval,
    )


def run_eastmoney_intraday_fixture_demo(
    config: AppConfig, fixture_file: Path
) -> IntradayProbeOutcome:
    """用保存样本执行完整双探针流程；不联网，且只写专用嵌目录新库。"""
    database = Database(config.storage.database)
    if database.path.name != "akshare-live-test.db" or database.path.parent.name != "akshare-probe-fixture":
        raise ConfigError(
            "C3 离线双探针只允许写入 runtime/akshare-probe-fixture/akshare-live-test.db。"
        )
    _require_models_closed(config)
    _require_current_schema(database)
    _require_fresh(database, "akshare-probe-fixture/akshare-live-test.db")
    if tuple(item.code for item in config.watchlist) != ("000001",):
        raise ConfigError("C3 离线双探针配置必须只观察代码 000001。")

    fixture = EastmoneyMinuteFixtureAdapter(fixture_file, confirmed_through=None)
    records = list(fixture.fetch_minute_bars(
        ("000001",), fixture.capture_start, fixture.capture_end
    ))
    raw_rows: list[dict[str, object]] = []
    for record in records:
        provider_raw = record.get("provider_raw")
        if not isinstance(provider_raw, dict):
            raise ConfigError("离线样本缺少可追溯原始行。")
        raw_rows.append(copy.deepcopy(provider_raw))

    def fixture_fetcher(**parameters: object) -> list[dict[str, object]]:
        start = str(parameters["start_date"])
        end = str(parameters["end_date"])
        return [
            copy.deepcopy(row) for row in raw_rows
            if start <= str(row["时间"]) <= end
        ]

    outcome = run_akshare_intraday_probe(
        config,
        fixture.capture_start.date(),
        "000001",
        confirmation=PROBE_CONFIRMATION,
        fetcher=fixture_fetcher,
        clock=ManualClock(fixture.capture_start.replace(hour=9, minute=40)),
        source=PROBE_FIXTURE_SOURCE,
        offline=True,
    )
    print("C3 离线双探针演示完成：真实网络请求 0 次，实际等待 0 秒。")
    print("独立离线库、实时独立库、主库和真实持仓之间没有数据复制或覆盖。")
    return outcome
