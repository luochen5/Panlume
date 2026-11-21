from __future__ import annotations

import gc
import tkinter as tk
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.schema import parse_config
from pandeng.desktop.i18n import normalize_language, translate, translate_observation_message
from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.settings import DesktopSettings, DesktopSettingsStore
from pandeng.desktop.window import PanlumeWindow
from pandeng.reporting.builder import build_daily_fact_package
from pandeng.reporting.models import resolve_model_recommendation
from pandeng.reporting.narrative import build_narrative_prompt, validate_narrative
from pandeng.reporting.presets import ReportPreset
from pandeng.reporting.templates import render_detailed_report
from pandeng.storage.db import Database


def test_traditional_and_japanese_resources_and_fallbacks() -> None:
    assert translate("报告中心", "zh-TW") == "報告中心"
    assert translate("数据质量", "zh-TW") == "數據質量"
    assert translate("报告中心", "ja") == "レポートセンター"
    assert translate("预览事实包摘要", "ja") == "ファクトパッケージをプレビュー"
    assert translate("添加、移除、启停和排序只改变观察清单，不会自行发起行情请求或提高频率。", "ja").startswith("Adding")
    assert translate_observation_message("第 2 轮观察中", "ja") == "第2ラウンドを監視中"
    assert normalize_language("zh-TW") == "zh-TW"
    assert normalize_language("ja") == "ja"
    with pytest.raises(ValueError, match="zh-CN、zh-TW、en 或 ja"):
        normalize_language("fr")
    assert ReportPreset(report_language="zh-TW").report_language == "zh-TW"
    assert ReportPreset(report_language="ja").report_language == "ja"


def demo_database(raw_config: dict, tmp_path: Path) -> Database:
    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    raw_config["models"]["ollama"]["enabled"] = False
    config = parse_config(raw_config, tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    run_analysis_demo(config, date(2026, 1, 7))
    return database


def test_traditional_and_japanese_reports_localize_fixed_text_but_preserve_original_alert(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    database = demo_database(raw_config, tmp_path)
    capsys.readouterr()
    facts = build_daily_fact_package(database, date(2026, 1, 7), "600000")
    original = facts.rule_events[0]["latest_message"]

    traditional = render_detailed_report(facts, "zh-TW")
    japanese = render_detailed_report(facts, "ja")

    assert "盤燈本地每日複盤" in traditional
    assert "數據狀態" in traditional
    assert "原始告警：" + original in traditional
    assert "プログラム事実" in japanese
    assert "データ状態" in japanese
    assert "ルール" in japanese
    assert "原始告警：" + original in japanese
    assert "原始告警：" + original in render_detailed_report(facts, "zh-CN")


def test_model_prompts_keep_fact_json_original_and_japanese_validation() -> None:
    from test_report_facts import package
    facts = package()
    preset = ReportPreset(model_name="qwen3:4b", report_language="ja")
    recommendation = resolve_model_recommendation(preset.model_name)

    traditional = build_narrative_prompt(facts, preset, recommendation, "zh-TW")
    japanese = build_narrative_prompt(facts, preset, recommendation, "ja")

    assert facts.canonical_json() in traditional
    assert facts.canonical_json() in japanese
    assert "不得新增事實" in traditional
    assert "事実の追加" in japanese
    valid = "600000 2026-01-07T09:33:00+08:00 イベント 2 active、1分の変化 1.25%、しきい値 1.0%。"
    assert validate_narrative(valid, facts, 400, "ja") == ()
    assert any("预测" in reason or "原因" in reason for reason in validate_narrative(
        valid + "必ず上がると予測する。", facts, 400, "ja"
    ))


def test_language_saved_for_restart_and_japanese_window_uses_central_resources(tmp_path) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    paths = DesktopPaths(tmp_path / "personal")
    app = PanlumeWindow(root, paths, default_language="en")
    try:
        app.language_var.set("日本語")
        with patch("pandeng.desktop.window.messagebox.showinfo"):
            app._save_language()
        assert app._ui_language == "en"
        assert app.store.load().language == "ja"
    finally:
        root.destroy()
        gc.collect()

    root = tk.Tk()
    try:
        app = PanlumeWindow(root, paths, default_language="en")
        assert app._ui_language == "ja"
        assert app.nav_buttons["reports"].cget("text") == "レポートセンター"
        assert app.nav_buttons["observation"].cget("text") == "監視設定"
    finally:
        root.destroy()
        gc.collect()
