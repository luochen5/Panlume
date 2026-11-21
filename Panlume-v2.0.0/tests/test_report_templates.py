from __future__ import annotations

from datetime import date
from pathlib import Path

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.schema import parse_config
from pandeng.reporting.builder import build_daily_fact_package, build_event_fact_package
from pandeng.reporting.templates import render_detailed_report, render_realtime_summary, render_report
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


def test_detailed_fixed_template_contains_complete_program_facts_without_model(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    facts = build_daily_fact_package(database, DAY, CODE)

    report = render_detailed_report(facts)
    repeated = render_report(build_daily_fact_package(database, DAY, CODE))

    assert report == repeated
    assert report.startswith("盘灯本地每日复盘：2026-01-07；600000")
    assert "分钟 240 条，已确认 240" in report
    assert "质量错误 0 条" in report
    assert "价格、成交量与日内高低点" in report
    assert "按时间排列的主要变化" in report
    assert "VWAP 估算" in report and "成交量比" in report
    assert "分钟 MACD/RSI" in report and "日线 MACD/RSI" in report
    assert "有效样本" in report and "macd_slow" in report
    assert "规则命中、解除、合并与冷却" in report
    assert "触发依据" not in report
    assert "条件 " in report and "实际 " in report and "阈值 " in report
    assert "重复合并" in report and "冷却至" in report
    assert "有数据边界的解释及相反可能" in report
    assert "数据不足与无法确认" in report
    assert "原始记录和计算结果的追溯信息" in report
    assert "分析输入 SHA-256" in report
    assert "本次本地复盘未调用 Ollama 或云端" in report


def test_english_detailed_template_uses_english_structure_and_preserves_records(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    facts = build_daily_fact_package(database, DAY, CODE)

    report = render_detailed_report(facts, "en")

    assert report.startswith("Panlume local daily review: 2026-01-07; 600000")
    assert "Data status ok" in report
    assert "Program facts | Data range and quality" in report
    assert "Rule triggers, resolution, merging, and cooldown" in report
    assert "Bounded interpretations and alternative possibilities" in report
    assert "Source records and calculation traceability" in report
    assert "No Ollama or cloud call was made" in report


def test_realtime_summary_names_event_basis_status_and_evidence(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    with database.connect() as connection:
        check_id = connection.execute(
            "SELECT id FROM rule_event_checks WHERE notified = 1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
    facts = build_event_fact_package(database, check_id)

    report = render_realtime_summary(facts)
    english = render_realtime_summary(facts, "en")

    assert facts.stock_code in report
    assert facts.rule_events[0]["event_name"] in report
    assert "当前" in report and "触发依据" in report
    assert "实际值" in report and "阈值" in report and "窗口" in report
    assert "首次" in report and "末次" in report and "解除" in report
    assert "重复合并" in report and "冷却至" in report
    assert "质量错误" in report and "来源" in report
    assert "追溯：事件" in report
    assert "不执行交易" in report
    assert "Basis:" in english and "Evidence event" in english
    assert "No trade is executed" in english


def test_empty_detailed_report_marks_unavailable_and_has_no_invented_event(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    facts = build_daily_fact_package(database, DAY, CODE)

    report = render_detailed_report(facts)
    english = render_detailed_report(facts, "en")

    assert "数据状态 unknown" in report
    assert "最近指标快照：无" in report
    assert "规则事件：0 条" in report
    assert "没有已保存规则事件" in report
    assert "不可用" in report
    assert "Data status unknown" in english
    assert "Latest indicator snapshot: none" in english
    assert "No saved rule events" in english
