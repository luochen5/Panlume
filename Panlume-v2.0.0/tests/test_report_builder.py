from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import RulesConfig, parse_config
from pandeng.data.base import MinuteBar
from pandeng.data.schedule import session_minutes
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.reporting.builder import build_daily_fact_package, build_event_fact_package
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes


DAY = date(2026, 1, 7)
CODE = "600000"


def demo_database(raw_config: dict, tmp_path: Path) -> Database:
    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    raw_config["models"]["ollama"]["enabled"] = False
    config = parse_config(raw_config, tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    run_analysis_demo(config, DAY)
    return database


def table_snapshot(database: Database) -> dict[str, list[tuple]]:
    with database.connect() as connection:
        names = [
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ) if row["name"] != "sqlite_sequence"
        ]
        return {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in names
        }


def test_daily_fact_package_is_complete_repeatable_and_read_only(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    before = table_snapshot(database)

    first = build_daily_fact_package(database, DAY, CODE)
    second = build_daily_fact_package(database, DAY, CODE)

    assert first.content_hash() == second.content_hash()
    assert first.canonical_json() == second.canonical_json()
    assert first.data_range["saved_minutes"] == 240
    assert first.data_quality["status"] == "ok"
    assert first.confirmed_status["daily_bar"] == "confirmed"
    assert first.price_volume_statistics["minute_bar_count"] == 240
    assert first.price_volume_statistics["daily"]["adjustment"] == "qfq"
    assert first.indicators["latest_snapshot"]["values"]["vwap_estimate"] is not None
    assert first.indicators["daily"]["ma20"] is not None
    assert first.indicators["momentum"]["minute"]["status"] == "ok"
    assert first.indicators["momentum"]["minute"]["adjustment"] == "none"
    assert first.indicators["momentum"]["daily"]["adjustment"] == "qfq"
    assert first.indicators["momentum"]["daily"]["sample_count"] == 25
    assert first.indicators["momentum"]["minute"]["parameters"]["macd_slow"] == 26
    assert first.rule_events
    assert any(event["merge_count"] > 0 for event in first.rule_events)
    assert any(event["cooldown_until"] for event in first.rule_events)
    assert first.traceability["minute_bar_ids"]
    assert table_snapshot(database) == before


def test_event_fact_package_traces_selected_check_and_rule_fields(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    with database.connect() as connection:
        check_id = connection.execute(
            "SELECT id FROM rule_event_checks WHERE notified = 1 ORDER BY id LIMIT 1"
        ).fetchone()[0]

    facts = build_event_fact_package(database, check_id)

    assert facts.report_type == "realtime_event_summary"
    assert facts.traceability["selected_event_check_id"] == check_id
    assert len(facts.rule_events) == 1
    event = facts.rule_events[0]
    assert event["event_id"] > 0
    assert event["condition_expression"] != "unknown"
    assert event["observed_values"]
    assert event["evidence_record_ids"]["event_check_ids"]
    assert event["evidence_record_ids"]["snapshot_ids"]
    with database.connect() as connection:
        for evidence_id in event["evidence_record_ids"]["event_check_ids"]:
            assert connection.execute(
                "SELECT 1 FROM rule_event_checks WHERE id = ?", (evidence_id,)
            ).fetchone()


def test_rule_event_reports_merge_cooldown_and_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "resolved.db")
    database.initialize()
    points = session_minutes(DAY)
    bars = [
        MinuteBar(CODE, points[index], close, close, close, close, 100, "test")
        for index, close in enumerate((11, 10, 9, 11))
    ]
    ingest_minutes(database, bars, adapter="test")
    add_position_lot(
        database, stock_code=CODE, quantity=100, buy_date=DAY.isoformat(),
        stop_loss_price=10, confirmation=CONFIRM_ADD,
    )
    analyzer = AnalysisService(
        database,
        rules=RulesConfig(rapid_move_pct=20, high_priority_repeat_seconds=900),
    )
    triggered = None
    for index in range(4):
        result = analyzer.analyze_code(CODE, points[index], analysis_mode="live")
        stops = [notice for notice in result.notices if notice.event_type == "fixed_stop"]
        if stops and triggered is None:
            triggered = stops[0]

    assert triggered is not None and triggered.event_check_id is not None
    facts = build_event_fact_package(database, triggered.event_check_id)
    event = facts.rule_events[0]
    assert event["status"] == "cleared"
    assert event["resolved_at"] == points[3].isoformat()
    assert event["last_triggered_at"] == points[2].isoformat()
    assert event["merge_count"] == 2
    assert event["cooldown_until"] == "2026-01-07T09:47:00+08:00"
    assert event["condition_expression"] == "close <= stop_loss_price"
    assert event["time_window"] == {"evaluation_period": "current_minute"}
    assert not any("未保存冷却秒数" in item for item in facts.limitations)
    assert facts.holdings_context[0]["confirmed_at"]


def test_empty_daily_fact_package_marks_missing_data_without_invention(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()

    facts = build_daily_fact_package(database, DAY, CODE)

    assert facts.data_quality["status"] == "unknown"
    assert facts.price_volume_statistics["close"] is None
    assert facts.indicators["latest_snapshot"] is None
    assert facts.rule_events == []
    assert any("没有已保存分钟线" in item for item in facts.limitations)
    assert any("日 K" in item for item in facts.limitations)


def test_event_builder_rejects_missing_check(tmp_path: Path) -> None:
    database = Database(tmp_path / "missing.db")
    database.initialize()
    with pytest.raises(ValueError, match="不存在"):
        build_event_fact_package(database, 999)
