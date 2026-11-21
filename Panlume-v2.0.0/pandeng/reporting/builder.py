"""从既有 SQLite 证据构建统一事实包，不重算规则、不写数据库。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from pandeng.config.schema import MomentumConfig, RulesConfig
from pandeng.data.schedule import CHINA_TZ, session_minutes
from pandeng.indicators.momentum import calculate_momentum
from pandeng.storage.db import Database

from .facts import FACT_PACKAGE_VERSION, FactPackage


EVENT_NAMES = {
    "rapid_rise": "快速上涨",
    "rapid_fall": "快速下跌",
    "volume_breakout": "放量突破",
    "below_vwap": "低于VWAP",
    "fixed_stop": "固定止损",
}
CONDITIONS = {
    "rapid_rise": "return_1m_pct >= threshold_pct",
    "rapid_fall": "return_1m_pct <= -threshold_pct",
    "volume_breakout": "close > prior_breakout_high AND volume_ratio >= volume_threshold",
    "below_vwap": "close < vwap_estimate",
    "fixed_stop": "close <= stop_loss_price",
}
THRESHOLD_FIELDS = {"threshold_pct", "volume_threshold", "stop_loss_price"}
PAYLOAD_METADATA = {
    "condition_expression", "time_window", "breakout_lookback_minutes", "cooldown_seconds",
}


def _rows(connection: sqlite3.Connection, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _row(connection: sqlite3.Connection, sql: str, parameters: tuple = ()) -> dict[str, Any] | None:
    row = connection.execute(sql, parameters).fetchone()
    return None if row is None else dict(row)


def _json(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _target_for_event(database: Database, event_check_id: int) -> tuple[str, str, int, int]:
    if isinstance(event_check_id, bool) or not isinstance(event_check_id, int) or event_check_id < 1:
        raise ValueError("事件检查编号必须是正整数。")
    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT c.event_id, c.snapshot_id, c.data_time, e.stock_code
            FROM rule_event_checks AS c
            JOIN rule_events AS e ON e.id = c.event_id
            WHERE c.id = ?
            """,
            (event_check_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"规则事件检查 {event_check_id} 不存在。")
    return row["stock_code"], row["data_time"], int(row["event_id"]), int(row["snapshot_id"])


def build_event_fact_package(
    database: Database,
    event_check_id: int,
    *,
    rules: RulesConfig | None = None,
    momentum: MomentumConfig | None = None,
) -> FactPackage:
    code, target, event_id, snapshot_id = _target_for_event(database, event_check_id)
    return _build_package(
        database, code, date.fromisoformat(target[:10]), target,
        report_type="realtime_event_summary", selected_event_id=event_id,
        selected_check_id=event_check_id, selected_snapshot_id=snapshot_id,
        rules=rules, momentum=momentum,
    )


def build_daily_fact_package(
    database: Database,
    session_date: date,
    stock_code: str,
    *,
    rules: RulesConfig | None = None,
    momentum: MomentumConfig | None = None,
) -> FactPackage:
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("报告股票代码必须是 6 位数字字符串。")
    target = session_minutes(session_date)[-1].isoformat()
    return _build_package(
        database, stock_code, session_date, target,
        report_type="daily_close_detail", rules=rules, momentum=momentum,
    )


def _build_package(
    database: Database,
    code: str,
    day: date,
    target: str,
    *,
    report_type: str,
    selected_event_id: int | None = None,
    selected_check_id: int | None = None,
    selected_snapshot_id: int | None = None,
    rules: RulesConfig | None = None,
    momentum: MomentumConfig | None = None,
) -> FactPackage:
    day_text = day.isoformat()
    rules = rules or RulesConfig()
    momentum = momentum or MomentumConfig()
    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        minutes = _rows(connection, """
            SELECT * FROM minute_bars
            WHERE stock_code = ? AND substr(bar_time, 1, 10) = ? AND bar_time <= ?
            ORDER BY bar_time, id
        """, (code, day_text, target))
        snapshots = _rows(connection, """
            SELECT s.*, r.analysis_mode, r.input_hash
            FROM indicator_snapshots AS s
            JOIN analysis_runs AS r ON r.id = s.analysis_run_id
            WHERE s.stock_code = ? AND s.session_date = ? AND s.data_time <= ?
            ORDER BY s.data_time, s.id
        """, (code, day_text, target))
        event_filter = "" if selected_event_id is None else " AND id = ?"
        parameters = (code, day_text) if selected_event_id is None else (code, day_text, selected_event_id)
        events = _rows(connection, f"""
            SELECT * FROM rule_events
            WHERE stock_code = ? AND session_date = ?{event_filter}
            ORDER BY id
        """, parameters)
        checks = _rows(connection, """
            SELECT c.* FROM rule_event_checks AS c
            JOIN rule_events AS e ON e.id = c.event_id
            WHERE e.stock_code = ? AND e.session_date = ?
            ORDER BY c.data_time, c.id
        """, (code, day_text))
        if selected_event_id is not None:
            checks = [item for item in checks if int(item["event_id"]) == selected_event_id]
        quality_issues = _rows(connection, """
            SELECT * FROM data_quality_issues
            WHERE (stock_code = ? OR stock_code IS NULL)
              AND substr(data_time, 1, 10) = ? AND data_time <= ?
            ORDER BY id
        """, (code, day_text, target))
        calendar = _row(
            connection, "SELECT * FROM trading_calendar WHERE trade_date = ?", (day_text,)
        )
        daily_bar = _row(connection, """
            SELECT * FROM daily_qfq_bars WHERE stock_code = ? AND trade_date = ?
        """, (code, day_text))
        daily_analysis = _row(connection, """
            SELECT * FROM daily_analysis WHERE stock_code = ? AND trade_date = ?
        """, (code, day_text))
        holdings = _rows(connection, """
            SELECT id, stock_code, quantity, buy_date, stop_loss_price, confirmed_at,
                   active, deactivated_at, deactivation_reason
            FROM position_lots WHERE stock_code = ? ORDER BY id
        """, (code,))
        recent_state = _row(connection, """
            SELECT last_contiguous_time, last_attempt_time, status, unknown_reason
            FROM monitor_code_states WHERE session_date = ? AND stock_code = ?
        """, (day_text, code))

    snapshot_by_id = {int(item["id"]): item for item in snapshots}
    checks_by_event: dict[int, list[dict[str, Any]]] = {}
    for check in checks:
        checks_by_event.setdefault(int(check["event_id"]), []).append(check)
    limitations: list[str] = []
    rule_events = [
        _rule_event(item, checks_by_event.get(int(item["id"]), []), snapshot_by_id, rules, limitations)
        for item in events
    ]

    actual_times = [item["bar_time"] for item in minutes]
    calendar_open = calendar is not None and bool(calendar["is_open"])
    expected = [] if calendar is not None and not calendar_open else [
        point.isoformat() for point in session_minutes(day) if point.isoformat() <= target
    ]
    missing = sorted(set(expected) - set(actual_times))
    extra = sorted(set(actual_times) - set(expected))
    confirmed = sum(bool(item["confirmed"]) for item in minutes)
    errors = [item for item in quality_issues if item["severity"] == "error"]
    info = [item for item in quality_issues if item["severity"] == "info"]

    if not minutes:
        limitations.append("所选范围没有已保存分钟线，价格、成交量和盘中指标不可用。")
    if missing:
        limitations.append(f"交易分钟缺失 {len(missing)} 条，首批缺失：{', '.join(missing[:3])}。")
    if extra:
        limitations.append(f"存在 {len(extra)} 条非预期交易分钟，不能视为连续完整序列。")
    if len(minutes) != confirmed:
        limitations.append(f"有 {len(minutes) - confirmed} 条分钟线尚未确认。")
    if errors:
        limitations.append(f"范围内存在 {len(errors)} 条数据质量错误，相关结论需谨慎复核。")
    if calendar is None:
        limitations.append("缺少所选交易日历，无法确认是否开市。")
    if recent_state and recent_state["unknown_reason"]:
        limitations.append("调度状态：" + recent_state["unknown_reason"])

    primary_snapshot = snapshot_by_id.get(selected_snapshot_id) if selected_snapshot_id else None
    if primary_snapshot is None and snapshots:
        primary_snapshot = snapshots[-1]
    if primary_snapshot is None:
        limitations.append("没有可用指标快照，VWAP、成交量比及其他已启用指标不可确认。")
    else:
        limitations.extend(_json(primary_snapshot["unknown_reasons_json"], []))

    if daily_bar is None:
        limitations.append("缺少当日前复权日 K，收盘日线状态不可用。")
    elif not bool(daily_bar["confirmed"]):
        limitations.append("当日日 K 尚未确认，收盘本身不代表日 K 已确认。")
    if daily_analysis is None:
        limitations.append("尚未保存每日分析，MA20 与正式双条件不可确认。")
    elif daily_analysis["unknown_reason"]:
        limitations.append(daily_analysis["unknown_reason"])

    price_volume = _price_volume(minutes, daily_bar)
    momentum_facts = {
        "minute": calculate_momentum(
            database, code, "minute", datetime.fromisoformat(target), momentum
        ).to_dict(),
    }
    if report_type == "daily_close_detail":
        momentum_facts["daily"] = calculate_momentum(
            database, code, "daily", day, momentum
        ).to_dict()
    indicator_facts = _indicator_facts(
        primary_snapshot, len(snapshots), daily_analysis, momentum_facts
    )
    timeline = _timeline(snapshots, checks, selected_snapshot_id, report_type)
    traceability = {
        "source_tables": [
            "minute_bars", "daily_qfq_bars", "data_quality_issues", "analysis_runs",
            "indicator_snapshots", "rule_events", "rule_event_checks", "daily_analysis",
        ],
        "minute_bar_ids": [int(item["id"]) for item in minutes],
        "daily_bar_ids": [] if daily_bar is None else [int(daily_bar["id"])],
        "quality_issue_ids": [int(item["id"]) for item in quality_issues],
        "snapshot_ids": [int(item["id"]) for item in snapshots],
        "analysis_run_ids": sorted({int(item["analysis_run_id"]) for item in snapshots}),
        "event_check_ids": [int(item["id"]) for item in checks],
        "rule_event_ids": [int(item["id"]) for item in events],
        "selected_event_check_id": selected_check_id,
        "analysis_input_hashes": {
            str(item["analysis_run_id"]): item["input_hash"] for item in snapshots
        },
    }
    return FactPackage(
        fact_package_version=FACT_PACKAGE_VERSION,
        report_type=report_type,
        stock_code=code,
        market_timezone="UTC+08:00",
        data_range={
            "session_date": day_text,
            "start": actual_times[0] if actual_times else None,
            "end": target,
            "latest_saved_minute": actual_times[-1] if actual_times else None,
            "expected_minutes_through_range": len(expected),
            "saved_minutes": len(minutes),
        },
        source_records={
            "sources": sorted({item["source"] for item in minutes}),
            "ingestion_run_ids": sorted({int(item["ingestion_run_id"]) for item in minutes}),
            "minute_bar_ids": [int(item["id"]) for item in minutes],
            "daily_bar_ids": [] if daily_bar is None else [int(daily_bar["id"])],
            "quality_issue_ids": [int(item["id"]) for item in quality_issues],
        },
        data_quality={
            "status": "ok" if minutes and not missing and not extra and not errors and confirmed == len(minutes) else "unknown",
            "errors": len(errors),
            "info": len(info),
            "missing_minutes": missing,
            "extra_minutes": extra,
            "issues": [
                {
                    "id": int(item["id"]), "data_kind": item["data_kind"],
                    "data_time": item["data_time"], "issue_type": item["issue_type"],
                    "severity": item["severity"], "message": item["message"],
                }
                for item in quality_issues
            ],
        },
        confirmed_status={
            "calendar": "unknown" if calendar is None else "open" if calendar_open else "closed",
            "minutes_saved": len(minutes),
            "minutes_confirmed": confirmed,
            "minutes_unconfirmed": len(minutes) - confirmed,
            "latest_snapshot": None if primary_snapshot is None else primary_snapshot["status"],
            "daily_bar": "missing" if daily_bar is None else "confirmed" if daily_bar["confirmed"] else "unconfirmed",
            "daily_analysis": None if daily_analysis is None else daily_analysis["status"],
        },
        price_volume_statistics=price_volume,
        timeline=timeline,
        indicators=indicator_facts,
        rule_events=rule_events,
        holdings_context=[
            {
                "lot_id": int(item["id"]), "quantity": int(item["quantity"]),
                "buy_date": item["buy_date"], "stop_loss_price": item["stop_loss_price"],
                "confirmed_at": item["confirmed_at"], "active": bool(item["active"]),
                "deactivated_at": item["deactivated_at"],
                "deactivation_reason": item["deactivation_reason"],
            }
            for item in holdings
        ],
        limitations=list(dict.fromkeys(str(item) for item in limitations)),
        traceability=traceability,
    )


def _rule_event(
    event: dict[str, Any],
    checks: list[dict[str, Any]],
    snapshots: dict[int, dict[str, Any]],
    rules: RulesConfig,
    limitations: list[str],
) -> dict[str, Any]:
    latest = checks[-1] if checks else None
    payload = {}
    for check in reversed(checks):
        candidate = _json(check["payload_json"], {})
        if candidate.keys() - {"unknown_reasons", "reason"}:
            payload = candidate
            break
    rule_id = event["event_type"]
    cooldown_seconds = payload.get("cooldown_seconds")
    if cooldown_seconds is None:
        cooldown_seconds = (
            rules.high_priority_repeat_seconds if event["priority"] == "high"
            else rules.normal_cooldown_seconds
        )
        limitations.append("部分历史规则检查未保存冷却秒数，冷却截止时间按当前规则配置计算。")
    cooldown_until = None
    if event["last_reminded_at"]:
        cooldown_until = (
            datetime.fromisoformat(event["last_reminded_at"]) + timedelta(seconds=cooldown_seconds)
        ).isoformat()
    time_window = payload.get("time_window")
    if time_window is None:
        time_window = {"evaluation_period": "1m"}
        if rule_id == "volume_breakout":
            lookback = payload.get("breakout_lookback_minutes")
            time_window["breakout_lookback_minutes"] = lookback
            if lookback is None:
                limitations.append("部分历史放量突破检查未保存回看根数，不能补写该时间窗口。")
        elif rule_id in {"below_vwap", "fixed_stop"}:
            time_window["evaluation_period"] = "session_to_current_minute" if rule_id == "below_vwap" else "current_minute"
    active_checks = [item for item in checks if item["is_active"] == 1]
    related_snapshots = [snapshots[int(item["snapshot_id"])] for item in checks if int(item["snapshot_id"]) in snapshots]
    source_bar_ids = sorted({
        int(bar_id)
        for snapshot in related_snapshots
        for bar_id in _json(snapshot["source_bar_ids_json"], [])
    })
    observed = {key: value for key, value in payload.items() if key not in THRESHOLD_FIELDS | PAYLOAD_METADATA}
    return {
        "event_id": int(event["id"]),
        "rule_id": rule_id,
        "event_name": EVENT_NAMES.get(rule_id, rule_id),
        "status": event["status"],
        "first_triggered_at": event["first_seen_at"],
        "last_triggered_at": active_checks[-1]["data_time"] if active_checks else event["first_seen_at"],
        "resolved_at": event["last_seen_at"] if event["status"] == "cleared" else None,
        "condition_expression": payload.get("condition_expression", CONDITIONS.get(rule_id, "unknown")),
        "observed_values": observed,
        "thresholds": {key: payload[key] for key in sorted(THRESHOLD_FIELDS) if key in payload},
        "time_window": time_window,
        "merge_count": max(0, int(event["check_count"]) - 1),
        "cooldown_until": cooldown_until,
        "evidence_record_ids": {
            "event_check_ids": [int(item["id"]) for item in checks],
            "snapshot_ids": sorted({int(item["snapshot_id"]) for item in checks}),
            "analysis_run_ids": sorted({int(item["analysis_run_id"]) for item in checks}),
            "minute_bar_ids": source_bar_ids,
        },
        "priority": event["priority"],
        "position_lot_id": event["position_lot_id"],
        "check_count": int(event["check_count"]),
        "reminder_count": int(event["reminder_count"]),
        "last_reminded_at": event["last_reminded_at"],
        "latest_is_active": None if latest is None else latest["is_active"],
        "latest_notification_kind": None if latest is None else latest["notification_kind"],
        "latest_message": event["latest_message"],
    }


def _price_volume(minutes: list[dict[str, Any]], daily_bar: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "minute_bar_count": len(minutes),
        "open": None, "close": None, "high": None, "low": None,
        "total_volume": 0, "vwap_estimate": None,
        "first_minute_bar_id": None, "latest_minute_bar_id": None,
        "daily": None,
    }
    if minutes:
        total_volume = sum(int(item["volume"]) for item in minutes)
        vwap = None
        if total_volume > 0:
            vwap = math.fsum(
                ((float(item["high"]) + float(item["low"]) + float(item["close"])) / 3.0)
                * int(item["volume"]) for item in minutes
            ) / total_volume
        result.update({
            "open": float(minutes[0]["open"]),
            "close": float(minutes[-1]["close"]),
            "high": max(float(item["high"]) for item in minutes),
            "low": min(float(item["low"]) for item in minutes),
            "total_volume": total_volume,
            "vwap_estimate": vwap,
            "first_minute_bar_id": int(minutes[0]["id"]),
            "latest_minute_bar_id": int(minutes[-1]["id"]),
        })
    if daily_bar is not None:
        result["daily"] = {
            "daily_bar_id": int(daily_bar["id"]),
            "open": float(daily_bar["open"]), "high": float(daily_bar["high"]),
            "low": float(daily_bar["low"]), "close": float(daily_bar["close"]),
            "volume": int(daily_bar["volume"]), "source": daily_bar["source"],
            "adjustment": daily_bar["adjustment"], "confirmed": bool(daily_bar["confirmed"]),
        }
    return result


def _indicator_facts(
    snapshot: dict[str, Any] | None,
    snapshot_count: int,
    daily: dict[str, Any] | None,
    momentum: dict[str, Any],
) -> dict[str, Any]:
    latest = None
    if snapshot is not None:
        latest = {
            "snapshot_id": int(snapshot["id"]),
            "analysis_run_id": int(snapshot["analysis_run_id"]),
            "data_time": snapshot["data_time"],
            "analysis_mode": snapshot["analysis_mode"],
            "status": snapshot["status"],
            "values": _json(snapshot["values_json"], {}),
            "unknown_reasons": _json(snapshot["unknown_reasons_json"], []),
            "source_bar_ids": _json(snapshot["source_bar_ids_json"], []),
            "latest_minute_bar_id": snapshot["latest_minute_bar_id"],
        }
    daily_facts = None
    if daily is not None:
        daily_facts = {
            "daily_analysis_id": int(daily["id"]), "status": daily["status"],
            "ma20": daily["ma20"], "previous_ma20": daily["previous_ma20"],
            "turn_days": daily["turn_days"], "close": daily["close"],
            "formal_signal": daily["formal_signal"], "analysis_mode": daily["analysis_mode"],
            "is_backfill": bool(daily["is_backfill"]), "unknown_reason": daily["unknown_reason"],
            "source_daily_bar_ids": _json(daily["source_daily_bar_ids_json"], []),
        }
    return {
        "snapshot_count": snapshot_count, "latest_snapshot": latest,
        "daily": daily_facts, "momentum": momentum,
    }


def _timeline(
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    selected_snapshot_id: int | None,
    report_type: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    selected_snapshots = snapshots
    if report_type == "realtime_event_summary":
        selected_snapshots = [item for item in snapshots if int(item["id"]) == selected_snapshot_id]
    for snapshot in selected_snapshots:
        values = _json(snapshot["values_json"], {})
        items.append({
            "time": snapshot["data_time"], "kind": "indicator_snapshot",
            "snapshot_id": int(snapshot["id"]), "analysis_run_id": int(snapshot["analysis_run_id"]),
            "close": values.get("close"), "return_1m_pct": values.get("return_1m_pct"),
            "return_5m_pct": values.get("return_5m_pct"),
            "volume_ratio": values.get("volume_ratio"), "vwap_estimate": values.get("vwap_estimate"),
        })
    for check in checks:
        items.append({
            "time": check["data_time"], "kind": "rule_check",
            "event_check_id": int(check["id"]), "event_id": int(check["event_id"]),
            "snapshot_id": int(check["snapshot_id"]), "analysis_run_id": int(check["analysis_run_id"]),
            "is_active": check["is_active"], "notified": bool(check["notified"]),
            "notification_kind": check["notification_kind"],
        })
    return sorted(items, key=lambda item: (item["time"], item["kind"], item.get("event_check_id", item.get("snapshot_id", 0))))
