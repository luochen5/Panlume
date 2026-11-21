"""C2 独立离线演示与一次显式真实分钟请求。

真实入口只允许 akshare-live-test.db，并要求逐字确认。主库、模型和持仓
均不在本模块的写入范围内。请求审计使用独立 JSON 文件，不改变数据库结构。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.storage.db import Database
from pandeng.storage.repositories import IngestionSummary, ingest_minutes

from .audit import AuditReport, audit_market_data
from .eastmoney_adapter import (
    INTERFACE_NAME,
    LIVE_SOURCE,
    VERIFIED_AKSHARE_VERSION,
    AkshareEastmoneyMinuteAdapter,
    EastmoneyMinuteFixtureAdapter,
    EastmoneyMinuteMapping,
    ProviderEmptyResponse,
    ProviderFormatError,
    ProviderLiveError,
)
from .response import ensure_minute_scope
from .schedule import CHINA_TZ, is_session_minute
from .provider_common import parse_china_provider_time


LIVE_CONFIRMATION = "确认请求一次东方财富公开分钟并写入独立测试库"


@dataclass(frozen=True)
class EastmoneyFixtureOutcome:
    ingestion: IngestionSummary
    raw_rows: int
    regular_rows: int
    excluded_0930: int
    confirmed_rows: int
    zero_volume_rows: int
    audit: AuditReport


@dataclass(frozen=True)
class EastmoneyLiveOutcome:
    ingestion: IngestionSummary
    mapping: EastmoneyMinuteMapping
    audit_path: Path
    audit: AuditReport
    request_count: int


def _require_models_closed(config: AppConfig) -> None:
    if config.ollama.enabled or config.cloud.enabled:
        raise ConfigError("C2/C3 行情验收要求 Ollama 和云端均关闭。")


def _require_current_schema(database: Database) -> None:
    if not database.is_initialized() or database.metadata("schema_version") != "6":
        raise ConfigError("请先用同一份专用配置运行 init；数据库结构版本必须为 6。")


def _require_fresh(database: Database, expected_name: str) -> None:
    tables = (
        "minute_bars", "daily_qfq_bars", "trading_calendar", "ingestion_runs",
        "monitor_rounds", "analysis_runs", "summary_tasks", "position_lots",
    )
    with database.connect() as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    if any(counts.values()):
        raise ConfigError(
            f"离线演示只使用全新的 {expected_name}；本次没有覆盖或追加。"
            "请保留旧库并先查看已有验收结果。"
        )


def run_eastmoney_fixture_demo(config: AppConfig, fixture_file: Path) -> EastmoneyFixtureOutcome:
    database = Database(config.storage.database)
    expected_name = "akshare-fixture-test.db"
    if database.path.name != expected_name:
        raise ConfigError(f"C2 离线演示只允许写入 {expected_name}，已拒绝其他数据库。")
    _require_models_closed(config)
    _require_current_schema(database)
    _require_fresh(database, expected_name)
    codes = tuple(item.code for item in config.watchlist)
    if codes != ("000001",):
        raise ConfigError("C2 离线样本配置必须只观察代码 000001。")

    print("盘灯 C2 离线验收：读取已保存的东方财富收盘后真实公开响应。")
    print("本次没有 HTTP 请求，不导入 AKShare，不调用 Ollama 或云端。")
    adapter = EastmoneyMinuteFixtureAdapter(fixture_file, confirmed_through=None)
    records = list(adapter.fetch_minute_bars(codes, adapter.capture_start, adapter.capture_end))
    ensure_minute_scope(records, codes, adapter.capture_start, adapter.capture_end)
    if len(records) != 240 or len(adapter.excluded_0930) != 1:
        raise ProviderFormatError("离线样本必须包含 240 条常规分钟和 1 条 09:30 排除记录。")

    summary = ingest_minutes(database, records, adapter=adapter.name, replay_clock=None)
    report = audit_market_data(database, codes, adapter.capture_start.date(), adapter.capture_end)
    confirmed = sum(bool(record["confirmed"]) for record in records)
    zero_volume = sum(int(record["volume"]) == 0 for record in records)
    print(
        f"1. 原始响应 241 行；常规分钟 {summary.received}；新增 {summary.accepted}；"
        f"09:30 特殊点单独保留 {len(adapter.excluded_0930)} 行。"
    )
    print("2. 保留 15:00；没有制造 13:00；09:30 没有改名为 09:31。")
    print(f"3. 成交量已按手乘 100 转为整数股；零成交量常规行 {zero_volume} 条，未删除。")
    print(
        f"4. confirmed_through 显式为 None；已确认 {confirmed}；未确认 {len(records) - confirmed}。"
    )
    print(
        f"5. 只读体检：分钟总体 {report.status}；完整 {report.complete_count} 只；"
        f"未知 {report.unknown_count} 只。未确认不能被表述为无触发或完整可用。"
    )
    print("原始服务商行保存在各分钟 raw_json.provider_raw；样本文件另保留完整 241 行。")
    print("未写主库、未写前复权日线或交易日历、未新增或修改真实持仓。")
    return EastmoneyFixtureOutcome(
        summary, 241, len(records), len(adapter.excluded_0930),
        confirmed, zero_volume, report,
    )


def _json_safe(value: object) -> object:
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (str, bytes, dict, list, tuple)):
        try:
            value = item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, (datetime, date, Path)):
        return value.isoformat() if not isinstance(value, Path) else str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_audit(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    content = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _rows_hash(rows: list[dict[str, object]]) -> str:
    normalized = json.dumps(
        _json_safe(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _request_failed_audit(
    path: Path,
    audit: dict[str, object],
    adapter: AkshareEastmoneyMinuteAdapter,
    category: str,
    error: BaseException,
) -> None:
    audit.update({
        "status": "request_failed",
        "error_category": category,
        "error_message": str(error)[:500],
        "request_count_actual": adapter.request_count,
        "raw_row_count": len(adapter.last_raw_rows),
        "raw_rows": adapter.last_raw_rows,
        "database_written": False,
    })
    _write_audit(path, audit)


def run_akshare_live_minute(
    config: AppConfig,
    session_date: date,
    stock_code: str,
    *,
    confirmation: str,
    confirmed_through: datetime | str | None,
    fetcher: Callable[..., object] | None = None,
    request_end: datetime | None = None,
    audit_path: Path | None = None,
    source: str = LIVE_SOURCE,
    offline: bool = False,
) -> EastmoneyLiveOutcome:
    """执行一次受控请求；离线样本模式仅供包内双探针演示使用。"""
    if confirmation != LIVE_CONFIRMATION:
        raise ConfigError("真实请求确认文本不匹配；没有发起请求或写数据库。")
    if offline and (fetcher is None or source == LIVE_SOURCE):
        raise ConfigError("离线样本模式必须提供本地响应和明确的离线来源。")
    if not offline and source != LIVE_SOURCE:
        raise ConfigError("真实请求模式必须使用明确的实时来源。")
    database = Database(config.storage.database)
    if database.path.name != "akshare-live-test.db":
        raise ConfigError("真实分钟请求只允许写入 akshare-live-test.db，已拒绝主库及其他数据库。")
    _require_models_closed(config)
    _require_current_schema(database)
    codes = tuple(item.code for item in config.watchlist)
    if codes != (stock_code,):
        raise ConfigError("真实测试配置的观察对象必须与命令中的单个代码完全一致。")

    start = datetime.combine(session_date, datetime.min.time()).replace(
        hour=9, minute=31, tzinfo=CHINA_TZ
    )
    end = datetime.combine(session_date, datetime.min.time()).replace(
        hour=15, minute=0, tzinfo=CHINA_TZ
    )
    if request_end is not None:
        try:
            end = parse_china_provider_time(request_end)
        except ProviderFormatError as exc:
            raise ConfigError(f"真实请求终点无效：{exc}") from exc
        if end.date() != session_date or not is_session_minute(end):
            raise ConfigError("真实请求终点必须是指定日期的盘灯交易整分钟。")
    request = {
        "symbol": stock_code,
        "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
        "period": "1",
        "adjust": "",
    }
    request_id = uuid.uuid4().hex
    audit_root = (database.path.parent / "akshare-live-audit").resolve()
    if audit_path is None:
        audit_path = audit_root / (
            f"eastmoney-minute-{stock_code}-{session_date.isoformat()}-{request_id[:12]}.json"
        )
    else:
        audit_path = audit_path.expanduser().resolve()
        if audit_path.parent != audit_root or audit_path.suffix != ".json":
            raise ConfigError("受控探针审计文件必须位于独立测试库的 akshare-live-audit 目录。")
        if audit_path.exists():
            raise ConfigError("受控探针审计文件已经存在；不会覆盖旧请求。")
    adapter = AkshareEastmoneyMinuteAdapter(
        confirmed_through=confirmed_through, fetcher=fetcher, source=source
    )
    audit: dict[str, object] = {
        "format": "pandeng.eastmoney-minute-request-audit.v1",
        "request_id": request_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "planned",
        "request_mode": "offline_fixture" if offline else "live",
        "source": source,
        "interface": INTERFACE_NAME,
        "akshare_version_required": VERIFIED_AKSHARE_VERSION,
        "request": request,
        "request_count_planned": 1,
        "request_count_actual": 0,
        "retry_count": 0,
        "confirmed_through": confirmed_through,
        "database": str(database.path),
        "database_written": False,
        "model_called": False,
        "position_written": False,
        "raw_rows": [],
    }
    _write_audit(audit_path, audit)
    if offline:
        print("离线双探针模拟请求：读取已保存样本，不导入 AKShare、不访问网络。")
        print(f"代码 {stock_code}；样本切片范围 {request['start_date']} 至 {request['end_date']}。")
        print("period=1；adjust=空字符串（不复权）；模拟请求 1 次；自动重试 0 次。")
    else:
        print("真实只读请求计划：东方财富公开 A 股一分钟，经 AKShare 调用。")
        print(f"代码 {stock_code}；范围 {request['start_date']} 至 {request['end_date']}。")
        print("period=1；adjust=空字符串（不复权）；预计请求 1 次；自动重试 0 次。")
        print("AKShare 高层接口未暴露本适配器的硬超时参数；不会把 YAML 的 20 秒配置当作强制超时。")
    print(f"唯一允许写入的数据库：{database.path}")
    print(f"请求审计：{audit_path}")

    try:
        records = list(adapter.fetch_minute_bars((stock_code,), start, end))
    except ProviderLiveError as exc:
        _request_failed_audit(audit_path, audit, adapter, exc.category, exc)
        raise ConfigError(f"真实请求分类 {exc.category}：{exc}") from exc
    except ProviderFormatError as exc:
        _request_failed_audit(audit_path, audit, adapter, "response_format", exc)
        raise
    except ValueError as exc:
        _request_failed_audit(audit_path, audit, adapter, "response_scope", exc)
        raise

    if len(adapter.last_mappings) != 1:
        error = ProviderFormatError("一次真实请求没有产生唯一映射结果。")
        _request_failed_audit(audit_path, audit, adapter, "response_format", error)
        raise error
    mapping = adapter.last_mappings[0]
    confirmed_count = sum(bool(record["confirmed"]) for record in records)
    audit.update({
        "status": "response_received",
        "request_count_actual": adapter.request_count,
        "raw_row_count": mapping.raw_row_count,
        "normalized_rows_sha256": _rows_hash(adapter.last_raw_rows),
        "regular_row_count": len(records),
        "confirmed_row_count": confirmed_count,
        "unconfirmed_row_count": len(records) - confirmed_count,
        "excluded_0930_count": len(mapping.excluded_0930),
        "excluded_0930": mapping.excluded_0930,
        "raw_rows": adapter.last_raw_rows,
    })
    # 原始响应先落审计，再进行范围检查和 SQLite 处理。
    _write_audit(audit_path, audit)
    try:
        ensure_minute_scope(records, (stock_code,), start, end)
        if not records:
            raise ProviderEmptyResponse()
        summary = ingest_minutes(database, records, adapter=adapter.name, replay_clock=None)
    except ProviderLiveError as exc:
        _request_failed_audit(audit_path, audit, adapter, exc.category, exc)
        raise ConfigError(f"真实请求分类 {exc.category}：{exc}") from exc
    except (ValueError, sqlite3.Error, OSError):
        audit["status"] = "processing_failed"
        _write_audit(audit_path, audit)
        raise

    audit["database_written"] = True
    audit["ingestion"] = asdict(summary)
    audit["status"] = "ingested" if summary.status == "ok" else "ingested_with_quality_errors"
    report = audit_market_data(database, (stock_code,), session_date, end)
    _write_audit(audit_path, audit)
    response_label = "离线样本响应" if offline else "真实响应"
    print(
        f"{response_label}：原始 {mapping.raw_row_count} 行；常规分钟 {len(records)}；"
        f"09:30 排除 {len(mapping.excluded_0930)}；请求 {adapter.request_count} 次。"
    )
    print(
        f"入库：新增 {summary.accepted}；去重 {summary.duplicates}；拒绝 {summary.rejected}；"
        f"{summary.status_text}。"
    )
    print(
        f"分钟确认：已确认 {confirmed_count}；未确认 {len(records) - confirmed_count}；"
        "最后一条已返回也不会自动确认。"
    )
    print(f"只读体检：总体 {report.status}；完整 {report.complete_count}；未知 {report.unknown_count}。")
    print("主库、真实持仓、Ollama 和云端均未修改或调用；没有自动重试。")
    return EastmoneyLiveOutcome(summary, mapping, audit_path, report, adapter.request_count)
