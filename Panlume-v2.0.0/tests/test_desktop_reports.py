from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.schema import parse_config
from pandeng.desktop.reports import DesktopReportService
from pandeng.reporting.presets import ReportPreset
from pandeng.storage.db import Database


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


def test_daily_preview_generate_export_and_raw_evidence(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    service = DesktopReportService(database)

    preview = service.preview(ReportPreset(), CODE, DAY.isoformat())
    report = service.generate(ReportPreset(), CODE, DAY.isoformat())
    json_path = service.export(report, tmp_path / "exports" / "report.json", "json")
    md_path = service.export(report, tmp_path / "exports" / "report.md", "markdown")
    evidence = service.raw_evidence(report)

    assert preview.summary == service._summary(report.package, "zh-CN")
    assert report.text == service.generate(ReportPreset(), CODE, DAY.isoformat()).text
    assert report.model_used is False and report.model_status == "not_requested"
    assert report.fallback_reason == "" and report.validation_result == "not_applicable"
    exported = json.loads(json_path.read_text(encoding="utf-8"))
    assert exported["format"] == "pandeng.report-export.v1"
    assert exported["fact_package"]["fact_package_version"] == "1.0"
    assert exported["fact_content_sha256"] == report.package.content_hash()
    assert exported["report"]["text"] == report.text
    assert md_path.read_text(encoding="utf-8") == report.text.rstrip() + "\n"
    assert '"raw_json"' in evidence and '"rule_event_checks"' in evidence


def test_realtime_event_choices_and_report_match_stock_and_date(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    service = DesktopReportService(database)
    choices = service.event_choices(CODE, DAY.isoformat())
    assert choices
    preset = replace(ReportPreset(), report_type="realtime_event_summary")

    report = service.generate(preset, CODE, DAY.isoformat(), choices[0].event_check_id)

    assert report.package.report_type == "realtime_event_summary"
    assert len(report.package.rule_events) == 1
    assert "触发依据" in report.text
    with pytest.raises(ValueError, match="不一致"):
        service.generate(preset, "000001", DAY.isoformat(), choices[0].event_check_id)
    with pytest.raises(ValueError, match="事件检查"):
        service.generate(preset, CODE, DAY.isoformat())


def test_display_preset_hides_sections_without_changing_fact_package(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    service = DesktopReportService(database)
    preset = replace(
        ReportPreset(), detail_level="brief", length_preference="short",
        show_data_quality=False, show_indicators=False, show_rule_basis=False,
        show_timeline=False, show_conditional_interpretation=False,
        show_limitations_traceability=False,
    )

    report = service.generate(preset, CODE, DAY.isoformat())

    assert "数据范围与质量" not in report.text
    assert "按时间排列的主要变化" not in report.text
    assert "已启用指标与日线结果" not in report.text
    assert "有数据边界的解释及相反可能" not in report.text
    assert "原始记录和计算结果的追溯信息" not in report.text
    assert "条件 " not in report.text
    assert report.package.data_range["saved_minutes"] == 240
    assert report.package.rule_events
    assert report.package.traceability["minute_bar_ids"]


def test_export_rejects_wrong_suffix_without_creating_file(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    service = DesktopReportService(database)
    report = service.generate(ReportPreset(), CODE, DAY.isoformat())

    with pytest.raises(ValueError, match="后缀"):
        service.export(report, tmp_path / "wrong.txt", "json")
    with pytest.raises(ValueError, match="后缀"):
        service.export(report, tmp_path / "wrong.txt", "markdown")
    assert not (tmp_path / "wrong.txt").exists()
