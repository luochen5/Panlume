from __future__ import annotations

import gc
import json
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

import pytest

from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.settings import DesktopSettings, DesktopSettingsStore
from pandeng.desktop.window import PanlumeWindow
from pandeng.summarizer.ollama import OllamaResponse
from test_desktop_history import populate_history


def make_app(tmp_path: Path, language: str = "zh-CN"):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    paths = DesktopPaths(tmp_path / "personal")
    paths.ensure_directories()
    DesktopSettingsStore(paths.settings_file).save(DesktopSettings(language=language))
    return root, PanlumeWindow(root, paths, default_language="en")


def wait_for_task(app, name: str) -> None:
    for _ in range(500):
        app.root.update()
        time.sleep(0.01)
        if name not in app.active_tasks:
            return
    pytest.fail(f"后台任务没有及时结束：{name}")


@pytest.fixture
def app(tmp_path):
    root, window = make_app(tmp_path)
    populate_history(window.data.database)
    yield window
    if root.winfo_exists():
        root.destroy()
    gc.collect()


def test_report_page_preview_generate_export_evidence_and_history(app, tmp_path) -> None:
    app.show_page("reports")
    page = app.report_page
    page.code_var.set("600000")
    page.date_var.set("2026-01-05")

    page.preview()
    wait_for_task(app, "report-preview")
    assert "内容 SHA-256" in page.preview_text.get("1.0", "end")
    page.generate()
    wait_for_task(app, "report-generate")

    assert "盘灯本地每日复盘" in page.report_text.get("1.0", "end")
    assert "原始记录和计算结果的追溯信息" in page.report_text.get("1.0", "end")
    assert page.current_report.model_used is False
    target = tmp_path / "ui-export.json"
    with patch("pandeng.desktop.report_page.filedialog.asksaveasfilename", return_value=str(target)):
        page.export("json")
    assert json.loads(target.read_text(encoding="utf-8"))["fact_package"]["stock_code"] == "600000"

    page.open_raw_evidence()
    wait_for_task(app, "report-evidence")
    dialogs = [item for item in app.root.winfo_children() if isinstance(item, tk.Toplevel)]
    assert len(dialogs) == 1
    evidence_text = next(
        child for child in dialogs[0].winfo_children()[0].winfo_children()
        if isinstance(child, tk.Text)
    )
    assert "raw_json" in evidence_text.get("1.0", "end")
    dialogs[0].destroy()

    page.open_history()
    wait_for_task(app, "event-history")
    assert app.current_page == "history"
    assert app.history_code_var.get() == "600000"
    assert app.history_date_var.get() == "2026-01-05"
    assert app.history_event_tree.selection()


def test_report_page_realtime_and_custom_preset_restore(app) -> None:
    app.show_page("reports")
    page = app.report_page
    page.code_var.set("600000")
    page.date_var.set("2026-01-05")
    page.type_var.set("实时事件摘要")

    page._report_type_changed()
    wait_for_task(app, "report-events")
    assert page.event_var.get()
    page.generate()
    wait_for_task(app, "report-generate")
    assert len(page.current_report.package.rule_events) == 1
    assert "触发依据" in page.report_text.get("1.0", "end")

    page.preset_name_var.set("UI 自定义")
    page.display_vars["show_data_quality"].set(False)
    page._save_preset()
    assert app.report_preset_store.load()[-1].show_data_quality is False
    page._restore_default()
    assert page.display_vars["show_data_quality"].get() is True
    assert "UI 自定义" in page.preset_names


def test_english_report_page_uses_translated_controls_and_template(tmp_path) -> None:
    root, app = make_app(tmp_path, "en")
    try:
        app.show_page("reports")
        page = app.report_page
        assert app.nav_buttons["reports"].cget("text") == "Reports"
        assert page.type_var.get() == "Single-stock close detail"
        page.code_var.set("600000")
        page.date_var.set("2026-01-07")
        page.generate()
        wait_for_task(app, "report-generate")
        assert page.report_text.get("1.0", "end").startswith("Panlume local daily review")
        assert "Model: not requested" in page.model_status_var.get()
    finally:
        root.destroy()
        gc.collect()



def test_report_page_displays_adopted_model_narrative_and_validation(app) -> None:
    class Client:
        def generate(self, prompt: str) -> OllamaResponse:
            facts = json.loads(prompt.split("只读事实包：", 1)[1])
            event = facts["rule_events"][0]
            text = (
                f"{facts['stock_code']} {facts['data_range']['session_date']} "
                f"事件 {event['event_id']} {event['status']}，"
                f"分钟收盘 {event['observed_values']['close']}，"
                f"人工确认止损价 {event['thresholds']['stop_loss_price']}。"
            )
            return OllamaResponse(text, "", "fake", json.dumps({"response": text}))

    app.report_service.narrative.client_factory = lambda config: Client()
    app.show_page("reports")
    page = app.report_page
    page.code_var.set("600000")
    page.date_var.set("2026-01-05")
    page.model_name_var.set("qwen3:4b")

    page.generate()
    wait_for_task(app, "report-generate")

    assert "模型：已采用" in page.model_status_var.get()
    assert "输出校验通过" in page.model_status_var.get()
    report = page.report_text.get("1.0", "end")
    assert "程序事实区" in report
    assert "可选模型叙述区" in report



def test_generated_report_opens_dedicated_results_page(app) -> None:
    app.show_page("reports")
    page = app.report_page
    page.code_var.set("600000")
    page.date_var.set("2026-01-05")

    page.generate()
    wait_for_task(app, "report-generate")

    assert app.current_page == "results"
    assert app.current_report is page.current_report
    results = app.report_results
    assert len(results.notebook.tabs()) == 3
    assert "盘灯本地每日复盘" in results.texts["full"].get("1.0", "end")
    assert "规则命中" in results.texts["facts"].get("1.0", "end")
    assert "没有采用模型叙述" in results.texts["narrative"].get("1.0", "end")
    assert "disabled" not in results.export_json_button.state()


def test_observation_summary_reuses_results_page_without_report_exports(app) -> None:
    from pandeng.desktop.summary import ObservationSummaryResult
    result = ObservationSummaryResult("600000：固定模板观察事实", False, "模型已停用")
    app.report_results.show_observation(result)
    app.show_page("results")

    assert app.current_report is None
    assert app.report_results.texts["full"].get("1.0", "end").startswith("600000")
    assert "disabled" in app.report_results.export_json_button.state()
