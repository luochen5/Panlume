"""只读上游证据、另存确定性本地复盘；默认不创建任何模型客户端。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from pandeng.config.schema import MomentumConfig
from pandeng.reporting.builder import build_daily_fact_package
from pandeng.reporting.facts import FactPackage
from pandeng.reporting.templates import render_detailed_report
from pandeng.storage.db import Database


REVIEW_FORMAT = "pandeng.daily-review-input.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def _json_field(item: dict | None, source: str, target: str) -> None:
    if item is None:
        return
    value = item.pop(source, None)
    item[target] = None if value is None else json.loads(value)


def _review_input(
    database: Database,
    session_date: date,
    stock_code: str,
    momentum: MomentumConfig | None = None,
) -> dict:
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("复盘股票代码必须是 6 位数字字符串。")
    day = session_date.isoformat()
    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        minutes = connection.execute(
            """
            SELECT COUNT(*) AS saved,
                   COALESCE(SUM(confirmed), 0) AS confirmed,
                   MIN(bar_time) AS first_time, MAX(bar_time) AS latest_time
            FROM minute_bars
            WHERE stock_code = ? AND substr(bar_time, 1, 10) = ?
            """,
            (stock_code, day),
        ).fetchone()
        sources = [
            row["source"] for row in connection.execute(
                """
                SELECT DISTINCT source FROM minute_bars
                WHERE stock_code = ? AND substr(bar_time, 1, 10) = ?
                ORDER BY source
                """,
                (stock_code, day),
            )
        ]
        calendar = _dict(connection.execute(
            "SELECT trade_date, is_open, source FROM trading_calendar WHERE trade_date = ?",
            (day,),
        ).fetchone())
        quality = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN severity = 'error' THEN 1 ELSE 0 END), 0) AS errors,
                COALESCE(SUM(CASE WHEN severity = 'info' THEN 1 ELSE 0 END), 0) AS info
            FROM data_quality_issues
            WHERE (stock_code = ? OR stock_code IS NULL)
              AND substr(data_time, 1, 10) = ?
            """,
            (stock_code, day),
        ).fetchone()
        quality_issues = [
            dict(row) for row in connection.execute(
                """
                SELECT data_kind, data_time, issue_type, severity, message
                FROM data_quality_issues
                WHERE stock_code = ? AND substr(data_time, 1, 10) = ?
                ORDER BY id DESC LIMIT 20
                """,
                (stock_code, day),
            )
        ]
        latest_snapshot = _dict(connection.execute(
            """
            SELECT s.id, s.data_time, s.status, s.latest_minute_bar_id,
                   s.values_json, s.unknown_reasons_json, s.source_bar_ids_json,
                   r.id AS analysis_run_id, r.analysis_mode
            FROM indicator_snapshots AS s
            JOIN analysis_runs AS r ON r.id = s.analysis_run_id
            WHERE s.session_date = ? AND s.stock_code = ?
            ORDER BY s.data_time DESC, s.id DESC LIMIT 1
            """,
            (day, stock_code),
        ).fetchone())
        for source, target in (
            ("values_json", "values"),
            ("unknown_reasons_json", "unknown_reasons"),
            ("source_bar_ids_json", "source_bar_ids"),
        ):
            _json_field(latest_snapshot, source, target)
        snapshot_count = int(connection.execute(
            """
            SELECT COUNT(*) FROM indicator_snapshots
            WHERE session_date = ? AND stock_code = ?
            """,
            (day, stock_code),
        ).fetchone()[0])
        events = [
            dict(row) for row in connection.execute(
                """
                SELECT id, event_type, priority, status, position_lot_id,
                       first_seen_at, last_seen_at, check_count, reminder_count,
                       latest_message
                FROM rule_events
                WHERE session_date = ? AND stock_code = ?
                ORDER BY id
                """,
                (day, stock_code),
            )
        ]
        daily_bar = _dict(connection.execute(
            """
            SELECT id, trade_date, open, high, low, close, volume,
                   source, adjustment, confirmed
            FROM daily_qfq_bars WHERE stock_code = ? AND trade_date = ?
            """,
            (stock_code, day),
        ).fetchone())
        daily_analysis = _dict(connection.execute(
            """
            SELECT id, status, ma20, previous_ma20, turn_days, close,
                   formal_signal, analysis_mode, is_backfill, unknown_reason,
                   source_daily_bar_ids_json
            FROM daily_analysis WHERE stock_code = ? AND trade_date = ?
            """,
            (stock_code, day),
        ).fetchone())
        _json_field(daily_analysis, "source_daily_bar_ids_json", "source_daily_bar_ids")
        summaries = [
            dict(row) for row in connection.execute(
                """
                SELECT t.id AS task_id, t.status, t.fallback_reason,
                       r.summary_kind, r.text, r.model_name
                FROM summary_tasks AS t
                JOIN rule_event_checks AS c ON c.id = t.event_check_id
                JOIN analysis_runs AS a ON a.id = c.analysis_run_id
                JOIN rule_events AS e ON e.id = c.event_id
                LEFT JOIN summary_results AS r ON r.task_id = t.id
                WHERE a.session_date = ? AND e.stock_code = ?
                ORDER BY t.id
                """,
                (day, stock_code),
            )
        ]
        recent_state = _dict(connection.execute(
            """
            SELECT last_contiguous_time, last_attempt_time, status, unknown_reason
            FROM monitor_code_states WHERE session_date = ? AND stock_code = ?
            """,
            (day, stock_code),
        ).fetchone())

    if calendar is None:
        calendar_status = "unknown"
    else:
        calendar_status = "open" if bool(calendar["is_open"]) else "closed"
    complete = (
        int(minutes["saved"]) > 0
        and calendar_status == "open"
        and int(quality["errors"]) == 0
        and latest_snapshot is not None
        and latest_snapshot["status"] == "ok"
        and daily_bar is not None
        and bool(daily_bar["confirmed"])
        and daily_analysis is not None
        and daily_analysis["status"] == "ok"
    )
    facts = {
        "format": REVIEW_FORMAT,
        "session_date": day,
        "stock_code": stock_code,
        "data_status": "ok" if complete else "unknown",
        "minutes": {
            "saved": int(minutes["saved"]),
            "confirmed": int(minutes["confirmed"]),
            "unconfirmed": int(minutes["saved"]) - int(minutes["confirmed"]),
            "first_time": minutes["first_time"],
            "latest_time": minutes["latest_time"],
            "sources": sources,
        },
        "calendar": calendar,
        "calendar_status": calendar_status,
        "quality": {"errors": int(quality["errors"]), "info": int(quality["info"])},
        "quality_issues": quality_issues,
        "snapshot_count": snapshot_count,
        "latest_snapshot": latest_snapshot,
        "recent_state": recent_state,
        "events": events,
        "daily_bar": daily_bar,
        "daily_analysis": daily_analysis,
        "local_summaries": summaries,
    }
    facts["fact_package"] = build_daily_fact_package(
        database, session_date, stock_code, momentum=momentum
    ).to_dict()
    return facts


def _local_report(facts: dict, language: str = "zh-CN") -> str:
    if "fact_package" in facts:
        return render_detailed_report(
            FactPackage.from_dict(facts["fact_package"]), language
        )
    if language == "en":
        return _local_report_en(facts)
    minutes = facts["minutes"]
    quality = facts["quality"]
    calendar_text = {"open": "明确开市", "closed": "明确休市", "unknown": "状态未知"}
    lines = [
        f"盘灯本地每日复盘：{facts['session_date']}；{facts['stock_code']}。",
        (
            f"数据状态 {facts['data_status']}；日历 {calendar_text[facts['calendar_status']]}；"
            f"分钟 {minutes['saved']} 条，已确认 {minutes['confirmed']}，"
            f"未确认 {minutes['unconfirmed']}；来源 {', '.join(minutes['sources']) or '无'}。"
        ),
        (
            f"最新分钟 {minutes['latest_time'] or '无'}；质量错误 {quality['errors']} 条；"
            f"信息与去重记录 {quality['info']} 条；已保存指标快照 {facts['snapshot_count']} 条。"
        ),
    ]
    snapshot = facts["latest_snapshot"]
    if snapshot is None:
        lines.append("最近指标快照：无；相关指标状态未知。")
    else:
        values = snapshot["values"]
        lines.append(
            f"最近指标快照 {snapshot['id']}：{snapshot['data_time']}；状态 {snapshot['status']}；"
            f"收盘 {values.get('close')}；VWAP 估算 {values.get('vwap_estimate')}；"
            f"成交量比 {values.get('volume_ratio')}；模式 {snapshot['analysis_mode']}。"
        )
    lines.append(f"规则事件：{len(facts['events'])} 条。")
    for event in facts["events"]:
        lines.append(
            f"  事件 {event['id']} {event['event_type']} {event['priority']} "
            f"{event['status']}；检查 {event['check_count']}；提醒 {event['reminder_count']}；"
            f"{event['latest_message']}"
        )
    daily = facts["daily_analysis"]
    if daily is None:
        lines.append("日线结论：尚未保存每日分析，状态未知；收盘本身不代表日 K 已确认。")
    else:
        signal = (
            "未知" if daily["formal_signal"] is None
            else "是" if bool(daily["formal_signal"]) else "否"
        )
        lines.append(
            f"日线结论：状态 {daily['status']}；MA20 {daily['ma20']}；"
            f"拐头天数 {daily['turn_days']}；正式双条件 {signal}；"
            f"补记={bool(daily['is_backfill'])}；原因 {daily['unknown_reason'] or '无'}。"
        )
    lines.append(f"本地摘要：{len(facts['local_summaries'])} 条。")
    for summary in facts["local_summaries"]:
        lines.append(
            f"  摘要任务 {summary['task_id']}：{summary['status']}；"
            f"类型 {summary['summary_kind'] or '尚无结果'}；内容 {summary['text'] or '无'}。"
        )
    lines.extend([
        "本报告只整理已保存事实，不重新计算指标，不产生实时提醒。",
        "本次本地复盘未调用 Ollama 或云端，不修改行情、规则、持仓，也不执行交易。",
    ])
    return "\n".join(lines)


def _local_report_en(facts: dict) -> str:
    """英文固定复盘模板；原始告警和既有摘要作为记录原文保留。"""
    minutes, quality = facts["minutes"], facts["quality"]
    calendar_text = {"open": "open", "closed": "closed", "unknown": "unknown"}
    lines = [
        f"Panlume local daily review: {facts['session_date']}; {facts['stock_code']}.",
        (
            f"Data status {facts['data_status']}; calendar {calendar_text[facts['calendar_status']]}; "
            f"minutes {minutes['saved']}, confirmed {minutes['confirmed']}, "
            f"unconfirmed {minutes['unconfirmed']}; sources {', '.join(minutes['sources']) or 'none'}."
        ),
        (
            f"Latest minute {minutes['latest_time'] or 'none'}; quality errors {quality['errors']}; "
            f"information/deduplication records {quality['info']}; saved indicator snapshots "
            f"{facts['snapshot_count']}."
        ),
    ]
    snapshot = facts["latest_snapshot"]
    if snapshot is None:
        lines.append("Latest indicator snapshot: none; related indicator status is unknown.")
    else:
        values = snapshot["values"]
        lines.append(
            f"Latest indicator snapshot {snapshot['id']}: {snapshot['data_time']}; "
            f"status {snapshot['status']}; close {values.get('close')}; estimated VWAP "
            f"{values.get('vwap_estimate')}; volume ratio {values.get('volume_ratio')}; "
            f"mode {snapshot['analysis_mode']}."
        )
    lines.append(f"Rule events: {len(facts['events'])}.")
    for event in facts["events"]:
        lines.append(
            f"  Event {event['id']} {event['event_type']} {event['priority']} {event['status']}; "
            f"checks {event['check_count']}; alerts {event['reminder_count']}.\n"
            f"  Original alert: {event['latest_message']}"
        )
    daily = facts["daily_analysis"]
    if daily is None:
        lines.append("Daily conclusion: no saved daily analysis; status unknown. Close alone does not confirm the daily bar.")
    else:
        signal = "unknown" if daily["formal_signal"] is None else str(bool(daily["formal_signal"])).lower()
        lines.append(
            f"Daily conclusion: status {daily['status']}; MA20 {daily['ma20']}; turn days "
            f"{daily['turn_days']}; formal dual condition {signal}; backfill={bool(daily['is_backfill'])}; "
            f"reason {daily['unknown_reason'] or 'none'}."
        )
    lines.append(f"Local summaries: {len(facts['local_summaries'])}.")
    for summary in facts["local_summaries"]:
        lines.append(
            f"  Summary task {summary['task_id']}: {summary['status']}; kind "
            f"{summary['summary_kind'] or 'no result'}; saved text {summary['text'] or 'none'}."
        )
    lines.extend([
        "This report only organizes saved facts. It does not recalculate indicators or emit live alerts.",
        "No Ollama or cloud call was made. Market data, rules, and holdings were not modified; no trade was executed.",
    ])
    return "\n".join(lines)


@dataclass(frozen=True)
class LocalReviewOutcome:
    review_id: int
    session_date: date
    stock_code: str
    data_status: str
    report: str
    created: bool
    input_hash: str


def create_local_review(
    database: Database,
    session_date: date,
    stock_code: str,
    language: str = "zh-CN",
    momentum: MomentumConfig | None = None,
) -> LocalReviewOutcome:
    """上游表只读；只在 review_runs 保存复盘，相同输入幂等返回。"""
    facts = _review_input(database, session_date, stock_code, momentum)
    input_json = json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False)
    input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    with database.connect() as connection:
        existing = connection.execute(
            """
            SELECT * FROM review_runs
            WHERE session_date = ? AND stock_code = ? AND input_hash = ?
            """,
            (session_date.isoformat(), stock_code, input_hash),
        ).fetchone()
        if existing is not None:
            return LocalReviewOutcome(
                int(existing["id"]), session_date, stock_code, existing["data_status"],
                existing["local_report"], False, input_hash,
            )
        report = _local_report(facts, language)
        cursor = connection.execute(
            """
            INSERT INTO review_runs(
                session_date, stock_code, input_hash, input_json,
                local_report, data_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_date.isoformat(), stock_code, input_hash, input_json,
                report, facts["data_status"], _now(),
            ),
        )
        review_id = int(cursor.lastrowid)
    return LocalReviewOutcome(
        review_id, session_date, stock_code, facts["data_status"], report, True, input_hash
    )


def load_review(database: Database, review_id: int) -> sqlite3.Row | None:
    with database.connect() as connection:
        return connection.execute(
            "SELECT * FROM review_runs WHERE id = ?", (review_id,)
        ).fetchone()


def recent_reviews(database: Database, limit: int = 20) -> list[sqlite3.Row]:
    with database.connect() as connection:
        return list(connection.execute(
            """
            SELECT r.*, c.status AS cloud_status, c.error_type AS cloud_error_type
            FROM review_runs AS r
            LEFT JOIN cloud_review_calls AS c ON c.review_id = r.id
            ORDER BY r.id DESC LIMIT ?
            """,
            (limit,),
        ))
