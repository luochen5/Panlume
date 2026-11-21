"""把指标快照、规则检查和调度分析门控连接起来。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pandeng.config.schema import RulesConfig
from pandeng.indicators.intraday import IntradayIndicators, calculate_intraday
from pandeng.rules.engine import RuleEngine, RuleNotice
from pandeng.storage.db import Database


@dataclass(frozen=True)
class AnalysisResult:
    analysis_run_id: int
    snapshot_id: int | None
    stock_code: str
    target_time: datetime
    status: str
    analysis_mode: str
    notices: tuple[RuleNotice, ...]
    skipped: bool = False


class AnalysisService:
    def __init__(
        self,
        database: Database,
        *,
        rules: RulesConfig | None = None,
        sector_map: dict[str, str] | None = None,
    ) -> None:
        self.database = database
        self.rules_config = rules or RulesConfig()
        self.sector_map = sector_map or {}
        self.rule_engine = RuleEngine(database, self.rules_config)

    @staticmethod
    def _input_hash(indicators: IntradayIndicators) -> str:
        content = json.dumps(
            {
                "stock_code": indicators.stock_code,
                "target_time": indicators.target_time.isoformat(),
                "status": indicators.status,
                "values": indicators.values,
                "unknown_reasons": indicators.unknown_reasons,
                "source_bar_ids": indicators.source_bar_ids,
            },
            ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def analyze_code(
        self,
        stock_code: str,
        target_time: datetime,
        *,
        analysis_mode: str,
        monitor_round_id: int | None = None,
    ) -> AnalysisResult:
        if analysis_mode not in {"live", "offline_recovery", "replay"}:
            raise ValueError("无效的分析模式。")
        indicators = calculate_intraday(
            self.database, stock_code, target_time,
            sector_code=self.sector_map.get(stock_code),
            breakout_lookback=self.rules_config.breakout_lookback_minutes,
        )
        day = indicators.target_time.date().isoformat()
        target = indicators.target_time.isoformat()
        input_hash = self._input_hash(indicators)
        with self.database.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM analysis_runs
                WHERE session_date = ? AND stock_code = ?
                  AND analysis_mode = ? AND target_time = ?
                """,
                (day, stock_code, analysis_mode, target),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise ValueError("同一代码、模式和目标时间的输入哈希发生变化，请检查数据冲突。")
                snapshot = connection.execute(
                    "SELECT id FROM indicator_snapshots WHERE analysis_run_id = ?",
                    (existing["id"],),
                ).fetchone()
                return AnalysisResult(
                    int(existing["id"]), None if snapshot is None else int(snapshot["id"]),
                    stock_code, indicators.target_time, existing["status"], analysis_mode, (), True,
                )
            now = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """
                INSERT INTO analysis_runs(
                    session_date, stock_code, target_time, analysis_mode,
                    monitor_round_id, status, input_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day, stock_code, target, analysis_mode, monitor_round_id,
                    indicators.status, input_hash, now,
                ),
            )
            run_id = int(cursor.lastrowid)
            snapshot_cursor = connection.execute(
                """
                INSERT INTO indicator_snapshots(
                    analysis_run_id, session_date, stock_code, data_time,
                    latest_minute_bar_id, status, unknown_reasons_json,
                    values_json, source_bar_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, day, stock_code, target, indicators.latest_minute_bar_id,
                    indicators.status,
                    json.dumps(indicators.unknown_reasons, ensure_ascii=False),
                    json.dumps(indicators.values, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(indicators.source_bar_ids), now,
                ),
            )
            snapshot_id = int(snapshot_cursor.lastrowid)

        notices = self.rule_engine.evaluate(
            indicators, snapshot_id=snapshot_id, analysis_run_id=run_id,
            analysis_mode=analysis_mode,
        )
        return AnalysisResult(
            run_id, snapshot_id, stock_code, indicators.target_time,
            indicators.status, analysis_mode, tuple(notices), False,
        )

    def analyze_round(self, round_result: Any) -> list[AnalysisResult]:
        mode = "offline_recovery" if bool(round_result.recovery) else "live"
        return [
            self.analyze_code(
                code, round_result.target, analysis_mode=mode,
                monitor_round_id=round_result.round_id,
            )
            for code in round_result.analysis_ready_codes
        ]
