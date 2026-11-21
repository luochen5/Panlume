"""确定性规则、逐批固定止损、事件合并与提醒频率控制。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pandeng.config.schema import RulesConfig
from pandeng.holdings.positions import t1_availability
from pandeng.indicators.intraday import IntradayIndicators
from pandeng.storage.db import Database


@dataclass(frozen=True)
class RuleNotice:
    event_id: int
    stock_code: str
    event_type: str
    priority: str
    notification_kind: str
    data_time: datetime
    message: str
    position_lot_id: int | None = None
    event_check_id: int | None = None


@dataclass(frozen=True)
class _Condition:
    fingerprint: str
    event_type: str
    priority: str
    active: bool | None
    message: str
    payload: dict
    position_lot_id: int | None = None


class RuleEngine:
    def __init__(self, database: Database, config: RulesConfig | None = None) -> None:
        self.database = database
        self.config = config or RulesConfig()

    @staticmethod
    def _number(values: dict, key: str) -> float | None:
        value = values.get(key)
        return None if value is None else float(value)

    def _conditions(self, indicators: IntradayIndicators) -> list[_Condition]:
        code = indicators.stock_code
        day = indicators.target_time.date().isoformat()
        stamp = indicators.target_time.isoformat()
        values = indicators.values
        close = self._number(values, "close")
        assert close is not None  # status=ok 的连续快照必有目标分钟收盘价
        conditions: list[_Condition] = []

        rapid = self._number(values, "return_1m_pct")
        for event_type, is_active, label in (
            ("rapid_rise", None if rapid is None else rapid >= self.config.rapid_move_pct, "快速上涨"),
            ("rapid_fall", None if rapid is None else rapid <= -self.config.rapid_move_pct, "快速下跌"),
        ):
            conditions.append(_Condition(
                f"{day}:{code}:{event_type}", event_type, "normal", is_active,
                (
                    f"{code} {stamp} {label}：1 分钟涨跌幅 "
                    f"{rapid:.4f}%，阈值 ±{self.config.rapid_move_pct:.4f}%。"
                    if rapid is not None else f"{code} {stamp} {label}指标未知。"
                ),
                {
                    "return_1m_pct": rapid, "threshold_pct": self.config.rapid_move_pct,
                    "condition_expression": (
                        "return_1m_pct >= threshold_pct" if event_type == "rapid_rise"
                        else "return_1m_pct <= -threshold_pct"
                    ),
                    "time_window": {"minutes": 1},
                },
            ))

        ratio = self._number(values, "volume_ratio")
        prior_high = self._number(values, "prior_breakout_high")
        breakout_active = None
        if ratio is not None and prior_high is not None:
            breakout_active = (
                close > prior_high and ratio >= self.config.volume_ratio_threshold
            )
        conditions.append(_Condition(
            f"{day}:{code}:volume_breakout", "volume_breakout", "normal", breakout_active,
            (
                f"{code} {stamp} 放量突破：收盘 {close:.4f}，此前高点 {prior_high:.4f}，"
                f"同时刻成交量比 {ratio:.4f}。"
                if breakout_active is not None else f"{code} {stamp} 放量突破指标未知。"
            ),
            {
                "close": close, "prior_breakout_high": prior_high,
                "volume_ratio": ratio, "volume_threshold": self.config.volume_ratio_threshold,
                "condition_expression": (
                    "close > prior_breakout_high AND volume_ratio >= volume_threshold"
                ),
                "breakout_lookback_minutes": self.config.breakout_lookback_minutes,
                "time_window": {
                    "evaluation_period": "1m",
                    "breakout_lookback_minutes": self.config.breakout_lookback_minutes,
                },
            },
        ))

        vwap = self._number(values, "vwap_estimate")
        below_vwap = None if vwap is None else close < vwap
        conditions.append(_Condition(
            f"{day}:{code}:below_vwap", "below_vwap", "normal", below_vwap,
            (
                f"{code} {stamp} 分钟收盘 {'低于' if below_vwap else '不低于'}典型价估算 VWAP："
                f"收盘 {close:.4f}，VWAP 估算 {vwap:.4f}。"
                if below_vwap is not None else f"{code} {stamp} VWAP 未知。"
            ),
            {
                "close": close, "vwap_estimate": vwap,
                "vwap_kind": "one_minute_typical_price_estimate",
                "condition_expression": "close < vwap_estimate",
                "time_window": {"evaluation_period": "session_to_current_minute"},
            },
        ))

        availability = t1_availability(self.database, code, indicators.target_time.date())
        availability_by_lot = {item.lot_id: item for item in availability.lots}
        active_lots = [
            row for row in self.database.list_position_lots()
            if row["stock_code"] == code and bool(row["active"])
            and row["stop_loss_price"] is not None
        ]
        for lot in active_lots:
            lot_id = int(lot["id"])
            stop = float(lot["stop_loss_price"])
            t1 = availability_by_lot.get(lot_id)
            triggered = close <= stop
            sellable = None if t1 is None else t1.sellable_quantity
            t1_status = "unknown" if t1 is None else t1.status
            message = (
                f"{code} {stamp} 固定止损{'触发' if triggered else '未触发'}：批次 {lot_id}，"
                f"分钟收盘 {close:.4f}，人工确认止损价 {stop:.4f}，"
                f"判定口径为收盘价小于或等于止损价（触及即提醒）。T+1 可卖状态 {t1_status}，"
                f"可卖数量 {sellable if sellable is not None else '未知'}。"
                "提醒受轮询和数据源延迟影响，仅作风险记录，不执行交易。"
            )
            conditions.append(_Condition(
                f"{day}:{code}:fixed_stop:{lot_id}", "fixed_stop", "high", triggered,
                message,
                {
                    "close": close, "stop_loss_price": stop, "quantity": int(lot["quantity"]),
                    "buy_date": lot["buy_date"], "t1_status": t1_status,
                    "sellable_quantity": sellable,
                    "latency_notice": "受轮询和数据源延迟影响",
                    "condition_expression": "close <= stop_loss_price",
                    "time_window": {"evaluation_period": "current_minute"},
                },
                lot_id,
            ))
        return conditions

    @staticmethod
    def _elapsed(last_reminded: str | None, checked_at: datetime) -> float | None:
        if last_reminded is None:
            return None
        return (checked_at - datetime.fromisoformat(last_reminded)).total_seconds()

    def _record(
        self,
        connection: sqlite3.Connection,
        condition: _Condition,
        indicators: IntradayIndicators,
        *,
        snapshot_id: int,
        analysis_run_id: int,
        analysis_mode: str,
        checked_at: datetime,
    ) -> RuleNotice | None:
        existing = connection.execute(
            "SELECT * FROM rule_events WHERE fingerprint = ? AND status = 'active'",
            (condition.fingerprint,),
        ).fetchone()
        data_time = indicators.target_time.isoformat()
        now = checked_at.isoformat()
        payload_data = dict(condition.payload)
        payload_data["cooldown_seconds"] = (
            self.config.high_priority_repeat_seconds
            if condition.priority == "high" else self.config.normal_cooldown_seconds
        )
        payload = json.dumps(payload_data, ensure_ascii=False, sort_keys=True)

        if condition.active is None:
            if existing is None:
                return None
            connection.execute(
                """
                INSERT INTO rule_event_checks(
                    event_id, snapshot_id, analysis_run_id, data_time, checked_at,
                    is_active, notified, notification_kind, payload_json
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?)
                """,
                (existing["id"], snapshot_id, analysis_run_id, data_time, now, payload),
            )
            connection.execute(
                "UPDATE rule_events SET check_count = check_count + 1, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
            return None

        if not condition.active:
            if existing is None:
                return None
            connection.execute(
                """
                UPDATE rule_events
                SET status = 'cleared', last_seen_at = ?, check_count = check_count + 1,
                    latest_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (data_time, condition.message, now, existing["id"]),
            )
            connection.execute(
                """
                INSERT INTO rule_event_checks(
                    event_id, snapshot_id, analysis_run_id, data_time, checked_at,
                    is_active, notified, notification_kind, payload_json
                ) VALUES (?, ?, ?, ?, ?, 0, 0, NULL, ?)
                """,
                (existing["id"], snapshot_id, analysis_run_id, data_time, now, payload),
            )
            return None

        event = existing
        if event is None:
            cursor = connection.execute(
                """
                INSERT INTO rule_events(
                    fingerprint, event_type, stock_code, session_date, status, priority,
                    position_lot_id, first_seen_at, last_seen_at, check_count,
                    reminder_count, latest_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, 1, 0, ?, ?, ?)
                """,
                (
                    condition.fingerprint, condition.event_type, indicators.stock_code,
                    indicators.target_time.date().isoformat(), condition.priority,
                    condition.position_lot_id, data_time, data_time, condition.message, now, now,
                ),
            )
            event = connection.execute(
                "SELECT * FROM rule_events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        else:
            connection.execute(
                """
                UPDATE rule_events
                SET last_seen_at = ?, check_count = check_count + 1,
                    latest_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (data_time, condition.message, now, event["id"]),
            )

        elapsed = self._elapsed(event["last_reminded_at"], checked_at)
        cooldown = (
            self.config.high_priority_repeat_seconds
            if condition.priority == "high" else self.config.normal_cooldown_seconds
        )
        live_notification = analysis_mode == "live" and (elapsed is None or elapsed >= cooldown)
        notification_kind = (
            "recovery" if analysis_mode != "live"
            else "repeat" if event["last_reminded_at"] is not None else "initial"
        )
        notified = int(live_notification)
        check_cursor = connection.execute(
            """
            INSERT INTO rule_event_checks(
                event_id, snapshot_id, analysis_run_id, data_time, checked_at,
                is_active, notified, notification_kind, payload_json
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                event["id"], snapshot_id, analysis_run_id, data_time, now,
                notified, notification_kind, payload,
            ),
        )
        if live_notification:
            connection.execute(
                """
                UPDATE rule_events
                SET last_reminded_at = ?, reminder_count = reminder_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, now, event["id"]),
            )
            return RuleNotice(
                int(event["id"]), indicators.stock_code, condition.event_type,
                condition.priority, notification_kind, indicators.target_time,
                condition.message, condition.position_lot_id, int(check_cursor.lastrowid),
            )
        return None

    def evaluate(
        self,
        indicators: IntradayIndicators,
        *,
        snapshot_id: int,
        analysis_run_id: int,
        analysis_mode: str,
        checked_at: datetime | None = None,
    ) -> list[RuleNotice]:
        """保存本次检查；恢复模式只补记，任何模式都不调用模型。"""
        if analysis_mode not in {"live", "offline_recovery", "replay"}:
            raise ValueError("无效的规则分析模式。")
        checked_at = (checked_at or indicators.target_time).astimezone(indicators.target_time.tzinfo)
        with self.database.connect() as connection:
            if indicators.status != "ok":
                active = list(connection.execute(
                    """
                    SELECT * FROM rule_events
                    WHERE stock_code = ? AND session_date = ? AND status = 'active'
                    """,
                    (indicators.stock_code, indicators.target_time.date().isoformat()),
                ))
                for event in active:
                    payload = json.dumps(
                        {"unknown_reasons": indicators.unknown_reasons}, ensure_ascii=False
                    )
                    connection.execute(
                        """
                        INSERT INTO rule_event_checks(
                            event_id, snapshot_id, analysis_run_id, data_time, checked_at,
                            is_active, notified, notification_kind, payload_json
                        ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?)
                        """,
                        (
                            event["id"], snapshot_id, analysis_run_id,
                            indicators.target_time.isoformat(), checked_at.isoformat(), payload,
                        ),
                    )
                    connection.execute(
                        "UPDATE rule_events SET check_count = check_count + 1, updated_at = ? WHERE id = ?",
                        (checked_at.isoformat(), event["id"]),
                    )
                return []
            conditions = self._conditions(indicators)
            known_fingerprints = {condition.fingerprint for condition in conditions}
            archived_stops = list(connection.execute(
                """
                SELECT fingerprint, position_lot_id FROM rule_events
                WHERE stock_code = ? AND session_date = ? AND status = 'active'
                  AND event_type = 'fixed_stop'
                """,
                (indicators.stock_code, indicators.target_time.date().isoformat()),
            ))
            for row in archived_stops:
                if row["fingerprint"] not in known_fingerprints:
                    conditions.append(_Condition(
                        row["fingerprint"], "fixed_stop", "high", False,
                        (
                            f"{indicators.stock_code} 批次 {row['position_lot_id']} 已归档或不再设置止损，"
                            "旧风险事件由人工持仓记录关闭。"
                        ),
                        {"reason": "position_lot_archived_or_stop_removed"},
                        row["position_lot_id"],
                    ))
            notices = [
                notice for condition in conditions
                if (notice := self._record(
                    connection, condition, indicators,
                    snapshot_id=snapshot_id, analysis_run_id=analysis_run_id,
                    analysis_mode=analysis_mode, checked_at=checked_at,
                )) is not None
            ]
        return notices
